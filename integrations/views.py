import json
import logging
import threading
from urllib.parse import urlencode

from django.conf import settings
from django.db import close_old_connections
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from integrations.models import Integration, SyncRun
from integrations.oauth import JobberOAuthError, create_authorization_url, exchange_code
from integrations.sync import JobberSyncService, disconnect_jobber
from integrations.webhooks import enqueue_webhook

logger = logging.getLogger(__name__)


def _active_jobber():
    return (
        Integration.objects.filter(provider=Integration.PROVIDER_JOBBER)
        .exclude(status=Integration.STATUS_DISCONNECTED)
        .order_by("-updated_at")
        .first()
    )


def _frontend_redirect(params: dict | None = None):
    base = settings.FRONTEND_URL.rstrip("/")
    if params:
        return HttpResponseRedirect(f"{base}/?{urlencode(params)}")
    return HttpResponseRedirect(f"{base}/")


def _start_sync(integration: Integration) -> SyncRun:
    if integration.last_error:
        integration.last_error = ""
        integration.save(update_fields=["last_error", "updated_at"])
    run = SyncRun.objects.create(integration=integration, kind=SyncRun.KIND_FULL, status=SyncRun.STATUS_QUEUED)

    def worker(run_id: int, integration_id: int):
        close_old_connections()
        try:
            integration_obj = Integration.objects.get(pk=integration_id)
            sync_run = SyncRun.objects.get(pk=run_id)
            JobberSyncService(integration_obj, sync_run=sync_run).run_full()
        except Exception:
            logger.exception("Background Jobber sync failed")
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker, args=(run.id, integration.id), daemon=True)
    thread.start()
    return run


def _latest_sync_run(integration: Integration):
    inflight = (
        integration.sync_runs.filter(status__in=[SyncRun.STATUS_QUEUED, SyncRun.STATUS_RUNNING])
        .order_by("-id")
        .first()
    )
    return inflight or integration.sync_runs.order_by("-id").first()


def _serialize_integration(integration: Integration | None):
    if not integration:
        return {
            "connected": False,
            "status": "disconnected",
            "webhook_url": settings.JOBBER.get("WEBHOOK_URL") or "",
        }
    latest = _latest_sync_run(integration)
    inflight = bool(latest and latest.status in {SyncRun.STATUS_QUEUED, SyncRun.STATUS_RUNNING})
    return {
        "connected": bool(integration.access_token)
        and integration.status != Integration.STATUS_DISCONNECTED,
        "status": "syncing" if inflight else integration.status,
        "account_id": integration.account_external_id,
        "account_name": integration.account_name,
        "scopes": integration.scopes,
        "last_synced_at": integration.last_synced_at.isoformat() if integration.last_synced_at else None,
        "token_expires_at": integration.access_token_expires_at.isoformat() if integration.access_token_expires_at else None,
        "last_error": "" if inflight else integration.last_error,
        "api_version": {
            "requested": integration.requested_api_version or settings.JOBBER["GRAPHQL_VERSION"],
            "served": integration.served_api_version,
            "warning": integration.version_warning,
        },
        "webhook_url": settings.JOBBER.get("WEBHOOK_URL") or "",
        "sync": {
            "id": latest.id,
            "status": latest.status,
            "entity_counts": latest.entity_counts,
            "error_message": latest.error_message,
            "started_at": latest.started_at.isoformat() if latest.started_at else None,
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
        }
        if latest
        else None,
    }


@require_GET
def connect(request):
    try:
        url = create_authorization_url()
    except JobberOAuthError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return HttpResponseRedirect(url)


@require_GET
def callback(request):
    error = request.GET.get("error")
    if error:
        return _frontend_redirect({"jobber": "error", "message": error})
    code = request.GET.get("code")
    if not code:
        return _frontend_redirect({"jobber": "error", "message": "missing_code"})
    try:
        integration = exchange_code(code, request.GET.get("state"))
        _start_sync(integration)
    except JobberOAuthError as exc:
        logger.exception("Jobber OAuth callback failed")
        return _frontend_redirect({"jobber": "error", "message": str(exc)})
    return _frontend_redirect({"jobber": "connected"})


@require_GET
def status(request):
    return JsonResponse({"ok": True, "jobber": _serialize_integration(_active_jobber())})


@csrf_exempt
@require_POST
def sync(request):
    integration = _active_jobber()
    if not integration or not integration.access_token:
        return JsonResponse({"ok": False, "error": "Jobber is not connected."}, status=400)
    if integration.sync_runs.filter(status__in=[SyncRun.STATUS_QUEUED, SyncRun.STATUS_RUNNING]).exists():
        return JsonResponse({"ok": False, "error": "A sync is already running."}, status=409)
    run = _start_sync(integration)
    return JsonResponse({"ok": True, "sync_id": run.id, "jobber": _serialize_integration(integration)})


@csrf_exempt
@require_POST
def disconnect(request):
    integration = _active_jobber()
    if not integration:
        return JsonResponse({"ok": True, "jobber": _serialize_integration(None)})
    disconnect_jobber(integration)
    return JsonResponse({"ok": True, "jobber": _serialize_integration(integration)})


@csrf_exempt
@require_POST
def webhook(request):
    hmac_header = request.headers.get("X-Jobber-Hmac-SHA256") or request.META.get("HTTP_X_JOBBER_HMAC_SHA256")
    try:
        event = enqueue_webhook(request.body, request.META.get("CONTENT_TYPE", ""), hmac_header)
    except PermissionError:
        return JsonResponse({"ok": False, "error": "invalid_signature"}, status=401)
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "invalid_payload"}, status=400)
    return JsonResponse({"ok": True, "duplicate": event is None})


urlpatterns = [
    path("connect/", connect, name="jobber-connect"),
    path("callback/", callback, name="jobber-callback"),
    path("webhooks/", webhook, name="jobber-webhook"),
    path("status/", status, name="jobber-status"),
    path("sync/", sync, name="jobber-sync"),
    path("disconnect/", disconnect, name="jobber-disconnect"),
]
