"""HttpOnly auth cookies for access + refresh tokens."""

from __future__ import annotations

from django.conf import settings


ACCESS_COOKIE = getattr(settings, "JWT_ACCESS_COOKIE_NAME", "peter_access")
REFRESH_COOKIE = getattr(settings, "JWT_REFRESH_COOKIE_NAME", "peter_refresh")


def _cookie_common() -> dict:
    secure = bool(getattr(settings, "JWT_COOKIE_SECURE", not settings.DEBUG))
    samesite = getattr(settings, "JWT_COOKIE_SAMESITE", "Lax")
    domain = getattr(settings, "JWT_COOKIE_DOMAIN", None) or None
    return {
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
        "domain": domain,
    }


def access_cookie_max_age() -> int:
    return int(getattr(settings, "JWT_ACCESS_TOKEN_MINUTES", 15) * 60)


def refresh_cookie_max_age() -> int:
    return int(getattr(settings, "JWT_REFRESH_TOKEN_DAYS", 30) * 24 * 3600)


def set_auth_cookies(response, *, access_token: str, refresh_token: str):
    """Attach access (site-wide) + refresh (auth paths only) HttpOnly cookies."""
    common = _cookie_common()
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=access_cookie_max_age(),
        path="/",
        **common,
    )
    # Narrow path so refresh raw value is only sent to account auth endpoints.
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=refresh_cookie_max_age(),
        path="/api/accounts/",
        **common,
    )
    return response


def clear_auth_cookies(response):
    common = _cookie_common()
    response.delete_cookie(ACCESS_COOKIE, path="/", samesite=common["samesite"], domain=common["domain"])
    response.delete_cookie(
        REFRESH_COOKIE,
        path="/api/accounts/",
        samesite=common["samesite"],
        domain=common["domain"],
    )
    # Also clear legacy path="/" refresh if an older build set it.
    response.delete_cookie(REFRESH_COOKIE, path="/", samesite=common["samesite"], domain=common["domain"])
    return response


def get_access_token(request) -> str:
    return (request.COOKIES.get(ACCESS_COOKIE) or "").strip()


def get_refresh_token(request, body: dict | None = None) -> str:
    raw = (request.COOKIES.get(REFRESH_COOKIE) or "").strip()
    if raw:
        return raw
    if body:
        return (body.get("refresh_token") or "").strip()
    return ""
