"""
User management for admins (Peter, Nov 2026: "add users myself and select what I can show them").

Only superusers (admins) can use it. Admins add people with a temporary password (the server sends
no email), choose which pages they may open (accounts.access) and their Financials role, set a new
temporary password, and disable or re-enable accounts. Accounts are never deleted: disabling keeps
their history (expenses they entered, reviews they made) and signs them out everywhere.
Admin accounts themselves can't be changed here, and nobody can change their own account here.
"""

from __future__ import annotations

import json
import secrets
import string

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.access import PAGES, allowed_pages
from accounts.models import RefreshToken, UserAccess
from financials.access import finance_role
from financials.models import ROLE_CHOICES, FinanceAccess

ACCESS_EVERYTHING = "everything"
ACCESS_PAGES = "pages"


def _error(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _admin_only(view):
    def wrapper(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and user.is_active and user.is_superuser):
            return _error("Only admins can manage users.", status=403)
        # Changes only as JSON: another website can't send that from an admin's browser without CORS
        # permission (on top of the SameSite sign-in cookie).
        if request.method in ("POST", "PATCH") and request.content_type != "application/json":
            return _error("Send the changes as JSON.", status=415)
        return view(request, *args, **kwargs)

    wrapper.csrf_exempt = True
    return wrapper


def _row(user) -> dict:
    pages = allowed_pages(user)
    return {
        "id": user.pk,
        "email": user.email or user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "name": user.get_full_name(),
        "is_active": user.is_active,
        "is_admin": user.is_superuser,
        "is_staff": user.is_staff and not user.is_superuser,
        "access": ACCESS_EVERYTHING if pages is None else ACCESS_PAGES,
        "pages": pages or [],
        "finance_role": finance_role(user),
        "last_login": user.last_login,
        "date_joined": user.date_joined,
    }


def _options() -> dict:
    return {
        "pages": [{"key": key, "group": group, "label": label} for key, (group, label) in PAGES.items()],
        "finance_roles": [{"key": "", "label": "No access"}] + [{"key": key, "label": label} for key, label in ROLE_CHOICES],
    }


def _clean_access(body) -> tuple[str, list[str]]:
    access = body.get("access")
    if access not in (ACCESS_EVERYTHING, ACCESS_PAGES):
        raise ValueError("Choose what this person can see: everything, or only some pages.")
    if access == ACCESS_EVERYTHING:
        return access, []
    pages = body.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("Pick at least one page, or choose “Everything”.")
    unknown = [page for page in pages if page not in PAGES]
    if unknown:
        raise ValueError("Unknown page.")
    return access, [page for page in PAGES if page in pages]  # stored in the Users page order


def _clean_role(body) -> str:
    role = body.get("finance_role") or ""
    if role and role not in dict(ROLE_CHOICES):
        raise ValueError("Unknown Financials access.")
    return role


def _clean_password(password, user=None) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("Enter a temporary password.")
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        raise ValueError(" ".join(exc.messages)) from exc
    return password


def _apply_access(user, access, pages, role, by):
    if access == ACCESS_EVERYTHING:
        UserAccess.objects.filter(user=user).delete()
    else:
        UserAccess.objects.update_or_create(user=user, defaults={"pages": pages})
    if role:
        FinanceAccess.objects.update_or_create(user=user, defaults={"role": role, "granted_by": by})
    else:
        FinanceAccess.objects.filter(user=user).delete()


def _sign_out_everywhere(user):
    RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())


def suggest_password() -> str:
    """Easy to read out or type on a phone: no 0/O or 1/l, with a digit and a symbol."""
    letters = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"
    body = "".join(secrets.choice(letters) for _ in range(10))
    return f"{body}{secrets.choice('23456789')}{secrets.choice('!@#$%')}"


@_admin_only
@require_http_methods(["GET", "POST"])
def users(request):
    User = get_user_model()
    if request.method == "GET":
        rows = User.objects.select_related("page_access", "finance_access").order_by("-is_active", "-is_superuser", "first_name", "email")
        return JsonResponse({"ok": True, "users": [_row(u) for u in rows], "options": _options(), "suggested_password": suggest_password()})

    body = _body(request)
    email = str(body.get("email") or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return _error("Enter a valid email address.")
    if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
        return _error("There's already an account with this email.")
    first = str(body.get("first_name") or "").strip()[:150]
    last = str(body.get("last_name") or "").strip()[:150]
    if not first:
        return _error("Enter a first name.")
    try:
        access, pages = _clean_access(body)
        role = _clean_role(body)
        password = _clean_password(body.get("password"), User(username=email, email=email, first_name=first, last_name=last))
    except ValueError as exc:
        return _error(str(exc))
    with transaction.atomic():
        user = User.objects.create_user(username=email, email=email, password=password, first_name=first, last_name=last)
        _apply_access(user, access, pages, role, request.user)
    user = User.objects.get(pk=user.pk)
    return JsonResponse({"ok": True, "user": _row(user)}, status=201)


@_admin_only
@require_http_methods(["PATCH"])
def user_detail(request, pk):
    User = get_user_model()
    user = User.objects.filter(pk=pk).first()
    if not user:
        return _error("User not found.", status=404)
    if user.pk == request.user.pk:
        return _error("You can't change your own account here.")
    if user.is_superuser:
        return _error("Admin accounts always see everything and can't be changed here.")
    body = _body(request)
    try:
        with transaction.atomic():
            fields = []
            if "first_name" in body or "last_name" in body:
                first = str(body.get("first_name", user.first_name) or "").strip()[:150]
                if not first:
                    raise ValueError("Enter a first name.")
                user.first_name, user.last_name = first, str(body.get("last_name", user.last_name) or "").strip()[:150]
                fields += ["first_name", "last_name"]
            if user.is_staff and body.get("access") == ACCESS_PAGES:
                # Staff always see everything (accounts.access): a page list would be saved but never apply.
                raise ValueError("Office staff accounts always see everything, so their pages can't be limited.")
            if "access" in body:
                access, pages = _clean_access(body)
                if access == ACCESS_EVERYTHING:
                    UserAccess.objects.filter(user=user).delete()
                else:
                    UserAccess.objects.update_or_create(user=user, defaults={"pages": pages})
            # Page access is left as it is when only the Financials role changes.
            if "finance_role" in body:
                role = _clean_role(body)
                if role:
                    FinanceAccess.objects.update_or_create(user=user, defaults={"role": role, "granted_by": request.user})
                else:
                    FinanceAccess.objects.filter(user=user).delete()
            signed_out = False
            if body.get("password"):
                user.set_password(_clean_password(body["password"], user))
                fields.append("password")
                signed_out = True
            if "is_active" in body:
                if not isinstance(body["is_active"], bool):
                    raise ValueError("is_active must be true or false.")
                user.is_active = body["is_active"]
                fields.append("is_active")
                signed_out = signed_out or not user.is_active
            if fields:
                user.save(update_fields=fields)
            if signed_out:
                _sign_out_everywhere(user)  # new password or disabled: other devices must sign in again
    except ValueError as exc:
        return _error(str(exc))
    user = User.objects.select_related("page_access", "finance_access").get(pk=user.pk)
    return JsonResponse({"ok": True, "user": _row(user)})


users_view = csrf_exempt(users)
user_detail_view = csrf_exempt(user_detail)
