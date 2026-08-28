import logging
import secrets
import hashlib
import base64
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations.crypto import decrypt_value, encrypt_value
from integrations.models import Integration, OAuthState

logger = logging.getLogger(__name__)


class JobberOAuthError(Exception):
    pass


def _cfg():
    return settings.JOBBER


def generate_pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def create_authorization_url():
    cfg = _cfg()
    if not cfg["CLIENT_ID"] or not cfg["REDIRECT_URI"]:
        raise JobberOAuthError("Jobber OAuth client is not configured.")

    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce()
    OAuthState.objects.create(state=state, code_verifier=verifier)

    params = {
        "response_type": "code",
        "client_id": cfg["CLIENT_ID"],
        "redirect_uri": cfg["REDIRECT_URI"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if cfg["SCOPES"]:
        params["scope"] = cfg["SCOPES"]

    return f"{cfg['AUTH_URL']}?{urlencode(params)}"


def _token_request(payload: dict) -> dict:
    cfg = _cfg()
    response = requests.post(
        cfg["TOKEN_URL"],
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"error": "invalid_response", "error_description": response.text[:500]}

    if response.status_code >= 400 or "access_token" not in body:
        message = body.get("error_description") or body.get("error") or response.text[:500]
        raise JobberOAuthError(f"Token request failed ({response.status_code}): {message}")
    return body


def _apply_tokens(integration: Integration, token_payload: dict) -> Integration:
    update_fields = [
        "access_token",
        "refresh_token",
        "token_type",
        "access_token_expires_at",
        "status",
        "last_error",
        "updated_at",
    ]
    if token_payload.get("warning"):
        logger.warning("Jobber token warning: %s", token_payload["warning"])
        meta = dict(integration.metadata or {})
        meta["refresh_token_warning"] = token_payload["warning"]
        integration.metadata = meta
        update_fields.append("metadata")
    expires_in = int(token_payload.get("expires_in") or 3600)
    integration.access_token = encrypt_value(token_payload["access_token"])
    if token_payload.get("refresh_token"):
        integration.refresh_token = encrypt_value(token_payload["refresh_token"])
    integration.token_type = token_payload.get("token_type") or "Bearer"
    integration.access_token_expires_at = timezone.now() + timedelta(seconds=max(expires_in - 90, 30))
    integration.status = Integration.STATUS_ACTIVE
    integration.last_error = ""
    integration.save(update_fields=update_fields)
    return integration


def exchange_code(code: str, state: str | None) -> Integration:
    cfg = _cfg()
    payload = {
        "client_id": cfg["CLIENT_ID"],
        "client_secret": cfg["CLIENT_SECRET"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["REDIRECT_URI"],
    }

    if state:
        pending = OAuthState.objects.filter(state=state, used_at__isnull=True).first()
        if not pending:
            raise JobberOAuthError("Invalid or expired OAuth state.")
        if pending.created_at < timezone.now() - timedelta(minutes=15):
            raise JobberOAuthError("OAuth state expired. Start connect again.")
        payload["code_verifier"] = pending.code_verifier
        pending.used_at = timezone.now()
        pending.save(update_fields=["used_at"])

    tokens = _token_request(payload)

    integration = (
        Integration.objects.filter(
            provider=Integration.PROVIDER_JOBBER,
            status__in=[Integration.STATUS_ACTIVE, Integration.STATUS_PENDING, Integration.STATUS_ERROR],
        )
        .order_by("-updated_at")
        .first()
    )
    if integration is None:
        integration = Integration(provider=Integration.PROVIDER_JOBBER)

    integration.scopes = cfg["SCOPES"]
    integration.status = Integration.STATUS_PENDING
    integration.save()
    return _apply_tokens(integration, tokens)


def get_access_token(integration: Integration) -> str:
    integration.refresh_from_db()
    if integration.is_token_fresh:
        return decrypt_value(integration.access_token)
    return refresh_access_token(integration)


def refresh_access_token(integration: Integration) -> str:
    cfg = _cfg()
    loaded_access_token = integration.access_token
    with transaction.atomic():
        locked = Integration.objects.select_for_update().get(pk=integration.pk)
        if locked.access_token != loaded_access_token and locked.is_token_fresh:
            integration.refresh_from_db()
            return decrypt_value(locked.access_token)
        if locked.is_token_fresh:
            integration.refresh_from_db()
            return decrypt_value(locked.access_token)

        refresh_token = decrypt_value(locked.refresh_token)
        if not refresh_token:
            locked.status = Integration.STATUS_ERROR
            locked.last_error = "Missing refresh token. Reconnect Jobber."
            locked.save(update_fields=["status", "last_error", "updated_at"])
            raise JobberOAuthError(locked.last_error)

        try:
            tokens = _token_request(
                {
                    "client_id": cfg["CLIENT_ID"],
                    "client_secret": cfg["CLIENT_SECRET"],
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
            )
        except JobberOAuthError as exc:
            locked.status = Integration.STATUS_ERROR
            locked.last_error = str(exc)
            locked.save(update_fields=["status", "last_error", "updated_at"])
            raise

        _apply_tokens(locked, tokens)
        integration.refresh_from_db()
        return decrypt_value(locked.access_token)


def mark_disconnected(integration: Integration):
    integration.status = Integration.STATUS_DISCONNECTED
    integration.access_token = ""
    integration.refresh_token = ""
    integration.access_token_expires_at = None
    integration.last_error = ""
    integration.save(
        update_fields=[
            "status",
            "access_token",
            "refresh_token",
            "access_token_expires_at",
            "last_error",
            "updated_at",
        ]
    )
