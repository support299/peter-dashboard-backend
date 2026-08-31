import json
import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from accounts.cookies import (
    clear_auth_cookies,
    get_refresh_token,
    set_auth_cookies,
)
from accounts.models import PasswordResetToken, RefreshToken
from accounts.tokens import issue_access_token, refresh_token_lifetime_days

logger = logging.getLogger(__name__)
User = get_user_model()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _user_payload(user):
    return {
        "id": user.pk,
        "email": user.email or user.username,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def _auth_json(request, user, *, message=None, extra=None):
    """Issue rotated cookie pair; tokens stay in HttpOnly cookies (not JSON)."""
    access = issue_access_token(user)
    _, refresh_raw = RefreshToken.issue(
        user,
        days=refresh_token_lifetime_days(),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        ip_address=_client_ip(request),
    )
    body = {
        "ok": True,
        "token_type": "Bearer",
        "expires_in": int(getattr(settings, "JWT_ACCESS_TOKEN_MINUTES", 15) * 60),
        "refresh_expires_in": refresh_token_lifetime_days() * 24 * 3600,
        "user": _user_payload(user),
        "auth_via": "cookie",
    }
    if message:
        body["message"] = message
    if extra:
        body.update(extra)
    response = JsonResponse(body)
    set_auth_cookies(response, access_token=access, refresh_token=refresh_raw)
    return response


@csrf_exempt
@require_POST
def login_view(request):
    body = _json_body(request)
    email = (body.get("email") or body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return JsonResponse({"ok": False, "error": "Email and password are required."}, status=400)

    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.filter(username__iexact=email).first()
    if user is None or not user.check_password(password) or not user.is_active:
        return JsonResponse({"ok": False, "error": "Invalid email or password."}, status=401)

    # Drop prior refresh sessions for this browser login (optional single-session feel:
    # keep others; only issue new pair for this client).
    login(request, user)
    return _auth_json(request, user)


@csrf_exempt
@require_POST
def refresh_view(request):
    body = _json_body(request)
    raw = get_refresh_token(request, body)
    if not raw:
        return JsonResponse({"ok": False, "error": "Refresh token is required."}, status=400)

    try:
        _, new_raw, user = RefreshToken.rotate(
            raw,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            ip_address=_client_ip(request),
        )
    except ValueError as exc:
        reason = str(exc)
        response = JsonResponse(
            {
                "ok": False,
                "error": (
                    "Refresh token reuse detected. All sessions revoked."
                    if reason == "reuse"
                    else "Invalid or expired refresh token."
                ),
            },
            status=401,
        )
        clear_auth_cookies(response)
        return response

    access = issue_access_token(user)
    login(request, user)
    response = JsonResponse(
        {
            "ok": True,
            "token_type": "Bearer",
            "expires_in": int(getattr(settings, "JWT_ACCESS_TOKEN_MINUTES", 15) * 60),
            "refresh_expires_in": refresh_token_lifetime_days() * 24 * 3600,
            "user": _user_payload(user),
            "auth_via": "cookie",
        }
    )
    set_auth_cookies(response, access_token=access, refresh_token=new_raw)
    return response


@csrf_exempt
@require_POST
def logout_view(request):
    body = _json_body(request)
    raw = get_refresh_token(request, body)
    if raw:
        row = RefreshToken.lookup(raw)
        if row and row.revoked_at is None:
            row.revoke()
    logout(request)
    response = JsonResponse({"ok": True})
    clear_auth_cookies(response)
    return response


@require_GET
def me_view(request):
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)
    return JsonResponse({"ok": True, "user": _user_payload(user)})


@csrf_exempt
@require_POST
def change_password_view(request):
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

    body = _json_body(request)
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""
    if not current_password or not new_password:
        return JsonResponse({"ok": False, "error": "Current and new password are required."}, status=400)
    if not user.check_password(current_password):
        return JsonResponse({"ok": False, "error": "Current password is incorrect."}, status=400)
    try:
        validate_password(new_password, user=user)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "error": " ".join(exc.messages)}, status=400)

    user.set_password(new_password)
    user.save(update_fields=["password"])
    RefreshToken.revoke_all_for_user(user)
    login(request, user)
    return _auth_json(request, user, message="Password updated.")


@csrf_exempt
@require_POST
def forgot_password_view(request):
    body = _json_body(request)
    email = (body.get("email") or "").strip().lower()
    generic = {
        "ok": True,
        "message": "If that email is registered, you will receive a reset link shortly.",
    }
    if not email or not EMAIL_RE.match(email):
        return JsonResponse(generic)

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if not user:
        user = User.objects.filter(username__iexact=email, is_active=True).first()
    if not user:
        return JsonResponse(generic)

    _, raw = PasswordResetToken.issue(user, hours=1)
    frontend = settings.FRONTEND_URL.rstrip("/")
    reset_url = f"{frontend}/?reset_token={raw}"
    subject = "Reset your Peter dashboard password"
    message = (
        "You requested a password reset for the Peter dashboard.\n\n"
        f"Open this link within 1 hour:\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )
    try:
        send_mail(
            subject,
            message,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            [user.email or email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send password reset email to %s", email)
        logger.warning("Password reset link for %s: %s", email, reset_url)
        if settings.DEBUG:
            generic["debug_reset_url"] = reset_url
        return JsonResponse(generic)

    if settings.DEBUG:
        logger.info("Password reset link for %s: %s", email, reset_url)
        generic["debug_reset_url"] = reset_url
    return JsonResponse(generic)


@csrf_exempt
@require_POST
def reset_password_view(request):
    body = _json_body(request)
    raw = (body.get("token") or "").strip()
    new_password = body.get("new_password") or ""
    if not raw or not new_password:
        return JsonResponse({"ok": False, "error": "Token and new password are required."}, status=400)

    row = PasswordResetToken.lookup(raw)
    if not row or not row.is_valid:
        return JsonResponse({"ok": False, "error": "Invalid or expired reset link."}, status=400)

    user = row.user
    try:
        validate_password(new_password, user=user)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "error": " ".join(exc.messages)}, status=400)

    user.set_password(new_password)
    user.save(update_fields=["password"])
    row.mark_used()
    RefreshToken.revoke_all_for_user(user)
    return JsonResponse({"ok": True, "message": "Password has been reset. You can sign in now."})
