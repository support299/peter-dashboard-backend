import base64
import hashlib
import hmac
import json
import logging
import threading
from urllib.parse import parse_qs

from django.conf import settings
from django.db import IntegrityError, close_old_connections
from django.utils import timezone

from integrations.mapping import parse_dt
from integrations.models import Integration, WebhookEvent
from integrations.oauth import mark_disconnected
from integrations.sync import JobberSyncService

logger = logging.getLogger(__name__)

DESTROY_ACTIONS = {"DESTROY", "DELETE", "DESTROYED", "REMOVE"}


def verify_webhook_signature(raw_body: bytes, hmac_header: str | None) -> bool:
    secret = settings.JOBBER["CLIENT_SECRET"]
    if not hmac_header or not secret or not raw_body:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest)
    provided = hmac_header.strip().encode("ascii")
    if len(expected) != len(provided):
        return False
    return hmac.compare_digest(expected, provided)


def event_key(topic: str, account_id: str, item_id: str, occurred_at) -> str:
    stamp = occurred_at.isoformat() if occurred_at else ""
    raw = f"{topic}|{account_id}|{item_id}|{stamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_webhook_payload(raw_body: bytes, content_type: str) -> dict:
    text = raw_body.decode("utf-8") if raw_body else ""
    if "application/x-www-form-urlencoded" in (content_type or ""):
        parsed = parse_qs(text, keep_blank_values=True)
        flat = {key: values[-1] if values else "" for key, values in parsed.items()}
        if "data" in flat:
            try:
                return json.loads(flat["data"])
            except json.JSONDecodeError:
                return {"data": {"webHookEvent": flat}}
        return {"data": {"webHookEvent": flat}}
    return json.loads(text) if text else {}


def extract_event(payload: dict) -> dict:
    event = ((payload or {}).get("data") or {}).get("webHookEvent") or {}
    occurred = event.get("occurredAt") or event.get("occuredAt")
    return {
        "topic": (event.get("topic") or "").upper(),
        "app_id": event.get("appId") or "",
        "account_id": event.get("accountId") or "",
        "item_id": event.get("itemId") or "",
        "occurred_at": parse_dt(occurred),
        "raw_event": event,
    }


def split_topic(topic: str) -> tuple[str, str]:
    parts = [part for part in (topic or "").split("_") if part]
    if len(parts) < 2:
        return topic or "", ""
    return "_".join(parts[:-1]), parts[-1]


def find_integration(account_id: str) -> Integration | None:
    qs = Integration.objects.filter(provider=Integration.PROVIDER_JOBBER)
    if account_id:
        match = qs.filter(account_external_id=account_id).first()
        if match:
            return match
    return qs.exclude(status=Integration.STATUS_DISCONNECTED).order_by("-updated_at").first()


def enqueue_webhook(raw_body: bytes, content_type: str, hmac_header: str | None) -> WebhookEvent | None:
    if not verify_webhook_signature(raw_body, hmac_header):
        raise PermissionError("Invalid Jobber webhook signature")

    payload = parse_webhook_payload(raw_body, content_type)
    info = extract_event(payload)
    if not info["topic"]:
        raise ValueError("Webhook payload missing topic")

    key = event_key(info["topic"], info["account_id"], info["item_id"], info["occurred_at"])
    integration = find_integration(info["account_id"])
    try:
        event = WebhookEvent.objects.create(
            integration=integration,
            event_key=key,
            topic=info["topic"],
            account_id=info["account_id"],
            item_id=info["item_id"],
            app_id=info["app_id"],
            occurred_at=info["occurred_at"],
            payload=payload,
            status=WebhookEvent.STATUS_QUEUED,
        )
    except IntegrityError:
        return None

    thread = threading.Thread(target=_process_event_safe, args=(event.id,), daemon=True)
    thread.start()
    return event


def _process_event_safe(event_id: int):
    close_old_connections()
    try:
        process_webhook_event(event_id)
    except Exception:
        logger.exception("Jobber webhook processing failed for %s", event_id)
        WebhookEvent.objects.filter(pk=event_id).update(
            status=WebhookEvent.STATUS_FAILED,
            error_message="Unhandled processing error",
            processed_at=timezone.now(),
        )
    finally:
        close_old_connections()


def process_webhook_event(event_id: int):
    event = WebhookEvent.objects.filter(pk=event_id).first()
    if not event or event.status == WebhookEvent.STATUS_PROCESSED:
        return

    topic = event.topic
    object_name, action = split_topic(topic)

    if topic == "APP_DISCONNECT":
        integration = event.integration or find_integration(event.account_id)
        if integration:
            mark_disconnected(integration)
        event.status = WebhookEvent.STATUS_PROCESSED
        event.processed_at = timezone.now()
        event.save(update_fields=["status", "processed_at"])
        return

    if topic in {"APP_CONNECT", "APP_UPDATE"}:
        event.status = WebhookEvent.STATUS_IGNORED
        event.processed_at = timezone.now()
        event.save(update_fields=["status", "processed_at"])
        return

    integration = event.integration or find_integration(event.account_id)
    if not integration or not integration.access_token:
        event.status = WebhookEvent.STATUS_FAILED
        event.error_message = "No connected Jobber account for this webhook."
        event.processed_at = timezone.now()
        event.save(update_fields=["status", "error_message", "processed_at"])
        return

    service = JobberSyncService(integration)
    try:
        if action in DESTROY_ACTIONS:
            service.delete_remote_node(object_name, event.item_id)
        else:
            service.apply_remote_node(object_name, event.item_id)
        event.status = WebhookEvent.STATUS_PROCESSED
        event.error_message = ""
    except Exception as exc:
        logger.exception("Jobber webhook %s failed", topic)
        event.status = WebhookEvent.STATUS_FAILED
        event.error_message = str(exc)
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "error_message", "processed_at"])
