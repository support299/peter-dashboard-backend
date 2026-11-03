"""Who can do what in Financials.

entry  – enter and correct their own expenses (no totals, profit or balances)
full   – see and manage all financial information
admin  – full, plus categories, user access and reopening reviewed months
Superusers are always admin. Everyone else has no access until granted.
"""

from __future__ import annotations

from functools import wraps

from accounts.authz import forbidden
from financials.models import ROLE_ADMIN, ROLE_ENTRY, ROLE_FULL

RANK = {"": 0, ROLE_ENTRY: 1, ROLE_FULL: 2, ROLE_ADMIN: 3}


def finance_role(user) -> str:
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    if user.is_superuser:
        return ROLE_ADMIN
    access = getattr(user, "finance_access", None)
    try:
        return access.role if access else ""
    except Exception:
        return ""


def has_role(user, level: str) -> bool:
    return RANK[finance_role(user)] >= RANK[level]


def can_see_finances(user) -> bool:
    """Totals, profit, payroll and balances (Master Dashboard included)."""
    return has_role(user, ROLE_FULL)


def require_role(level: str):
    """View decorator; use inside accounts.authz.protect, which sets request.user."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not has_role(request.user, level):
                return forbidden("You don't have access to this part of Financials.")
            return view(request, *args, **kwargs)

        wrapper.csrf_exempt = getattr(view, "csrf_exempt", False)
        return wrapper

    return decorator
