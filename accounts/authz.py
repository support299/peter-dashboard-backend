"""API authentication / authorization helpers (defense in depth with middleware)."""

from __future__ import annotations

from functools import wraps

from django.http import JsonResponse

from accounts.tokens import user_from_access_token

# Exact paths that skip login (still may have their own checks — e.g. webhook HMAC).
PUBLIC_API_PATHS = frozenset(
    {
        "/api/accounts/login/",
        "/api/accounts/refresh/",
        "/api/accounts/forgot-password/",
        "/api/accounts/reset-password/",
        "/api/accounts/logout/",  # accepts refresh_token body when access expired
        "/api/jobber/webhooks/",
        "/api/jobber/callback/",
    }
)

# Privileged mutating / ops endpoints — must be staff (or superuser).
STAFF_ONLY_PREFIXES = (
    "/api/operations/celery/",
    "/api/jobber/disconnect/",
    "/api/operations/cancellations/process/",
)


def resolve_api_user(request):
    """
    Authenticate from (in order):
    1. HttpOnly access cookie
    2. Authorization: Bearer <jwt> (optional for non-browser clients)
    3. Django session (Jobber OAuth connect link)
    """
    from accounts.cookies import get_access_token

    cookie_token = get_access_token(request)
    if cookie_token:
        try:
            user = user_from_access_token(cookie_token)
        except Exception:
            user = None
        if user is not None and user.is_active:
            return user

    header = request.META.get("HTTP_AUTHORIZATION", "")
    if header.startswith("Bearer "):
        token = header[7:].strip()
        if token:
            try:
                user = user_from_access_token(token)
            except Exception:
                return None
            if user is not None and user.is_active:
                return user
            return None

    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False) and user.is_active:
        return user
    return None


def is_public_api_path(path: str) -> bool:
    if path in PUBLIC_API_PATHS:
        return True
    # Trailing-slash variants already normalized by CommonMiddleware usually.
    if not path.endswith("/") and f"{path}/" in PUBLIC_API_PATHS:
        return True
    return False


def requires_staff_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in STAFF_ONLY_PREFIXES)


def unauthorized(message="Authentication required.", status=401):
    return JsonResponse({"ok": False, "error": message}, status=status)


def forbidden(message="You do not have permission to perform this action."):
    return JsonResponse({"ok": False, "error": message}, status=403)


def require_api_auth(view=None, *, staff=False):
    """Decorator: require authenticated active user; optionally staff."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(request, *args, **kwargs):
            user = resolve_api_user(request)
            if user is None:
                return unauthorized()
            request.user = user
            if staff and not (user.is_staff or user.is_superuser):
                return forbidden()
            return fn(request, *args, **kwargs)

        # Preserve Django's csrf_exempt flag on the outermost callable.
        wrapper.csrf_exempt = getattr(fn, "csrf_exempt", False)
        return wrapper

    if view is not None:
        return decorator(view)
    return decorator


def require_api_staff(view):
    return require_api_auth(view, staff=True)


def protect(view, *, staff=False):
    """Wrap a view for use in urlpatterns."""
    return require_api_auth(view, staff=staff)
