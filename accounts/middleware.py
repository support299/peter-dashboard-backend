from django.http import JsonResponse

from accounts.authz import (
    is_public_api_path,
    requires_staff_path,
    resolve_api_user,
)


class JWTAuthenticationMiddleware:
    """
    Gate every /api/* route:
    - public paths: pass through (login, refresh, forgot/reset, OAuth callback, signed webhooks)
    - everything else: require active authenticated user (JWT Bearer or session)
    - staff-only prefixes: require is_staff / superuser
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        if not path.startswith("/api/"):
            return self.get_response(request)

        if is_public_api_path(path):
            return self.get_response(request)

        user = resolve_api_user(request)
        if user is None:
            header = request.META.get("HTTP_AUTHORIZATION", "")
            if header.startswith("Bearer "):
                return JsonResponse(
                    {"ok": False, "error": "Invalid or expired access token."},
                    status=401,
                )
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)

        request.user = user

        if requires_staff_path(path) and not (user.is_staff or user.is_superuser):
            return JsonResponse(
                {"ok": False, "error": "You do not have permission to perform this action."},
                status=403,
            )

        return self.get_response(request)
