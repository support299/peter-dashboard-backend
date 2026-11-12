"""
Which pages a user may open (Peter, Nov 2026: choose what each person can see; Cassidy = Manager view only).

A user with no UserAccess row, or an empty page list, keeps full access, as before. Staff and
superusers always have full access. A user with pages listed may only open those pages: the API
refuses everything else, so typing another address doesn't help. A list that's filled in but
unreadable (a typo, plain text) locks the user out rather than letting them see everything.
Financials are separate: they follow the Financials role (financials.access), for everyone.

Pages are "master:<page>" for Master Dashboard pages, or a whole section ("jobber", "internal", "pricing").
"""

from __future__ import annotations

# key -> (group, label). Order = order shown on the Users page.
PAGES = {
    "master:overview": ("Master Dashboard", "CEO view"),
    "master:manager": ("Master Dashboard", "Manager overview"),
    "master:revenue": ("Master Dashboard", "Revenue"),
    "master:clients": ("Master Dashboard", "Clients & growth"),
    "master:mrr": ("Master Dashboard", "MRR"),
    "master:sales": ("Master Dashboard", "Sales & leads"),
    "master:cancellations": ("Master Dashboard", "Cancellations"),
    "master:operations": ("Master Dashboard", "Operations"),
    "master:quality": ("Master Dashboard", "Quality"),
    "master:alerts": ("Master Dashboard", "Alerts"),
    "jobber": ("Other sections", "Jobber"),
    "internal": ("Other sections", "Internal App (Hub)"),
    "pricing": ("Other sections", "Pricing Calculator"),
}

_INSIGHTS = "/api/insights/"
# API paths each page needs. Lists and trend charts are checked again per number (insights.views).
PAGE_API = {
    "master:overview": (_INSIGHTS + "overview/", _INSIGHTS + "trend/", _INSIGHTS + "drill/"),
    "master:manager": (_INSIGHTS + "manager/", _INSIGHTS + "drill/"),
    "master:revenue": (_INSIGHTS + "revenue/", _INSIGHTS + "trend/", _INSIGHTS + "drill/"),
    "master:clients": (_INSIGHTS + "clients/", _INSIGHTS + "trend/", _INSIGHTS + "drill/"),
    "master:mrr": (_INSIGHTS + "mrr/", _INSIGHTS + "trend/", _INSIGHTS + "drill/"),
    "master:sales": (_INSIGHTS + "sales/", _INSIGHTS + "trend/", _INSIGHTS + "drill/"),
    "master:cancellations": (_INSIGHTS + "cancellations/", _INSIGHTS + "trend/", _INSIGHTS + "drill/"),
    "master:operations": (_INSIGHTS + "operations/", _INSIGHTS + "drill/"),
    "master:quality": (_INSIGHTS + "quality/", _INSIGHTS + "drill/"),
    "master:alerts": (_INSIGHTS + "alerts/",),
    "jobber": ("/api/operations/", "/api/jobber/"),
    "internal": ("/api/admin-internal/",),
    "pricing": ("/api/pricing-calculator/",),
}

# Needed by every signed-in user: who am I, change password, sign out, and the "Updated 2h ago" line.
# (The user management API under /api/accounts/users/ checks for an admin itself.)
ALWAYS_ALLOWED = ("/api/accounts/", "/api/jobber/status/")

# Never for limited users, even with the section: refreshing data from the source, connecting accounts.
FULL_ACCESS_ONLY = (
    "/api/jobber/sync/",
    "/api/jobber/connect/",
    "/api/jobber/disconnect/",
    "/api/admin-internal/sync/",
    "/api/pricing-calculator/sync/",
)


def allowed_pages(user) -> list[str] | None:
    """None = everything; otherwise the pages this user may open (possibly none)."""
    if user is None or user.is_staff or user.is_superuser:
        return None
    access = getattr(user, "page_access", None)  # no row: AttributeError subclass -> None
    if access is None:
        return None
    raw = access.pages
    if raw in (None, "", [], {}):
        return None  # explicitly empty = everything
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []  # unreadable: lock down, never open up
    return [page for page in raw if isinstance(page, str) and page in PAGES]


def api_allowed(user, path: str) -> bool:
    pages = allowed_pages(user)
    if pages is None:
        return True
    if path.startswith(ALWAYS_ALLOWED):
        return True
    if path.startswith(FULL_ACCESS_ONLY):
        return False
    if path.startswith("/api/financials/"):
        from financials.access import finance_role

        return bool(finance_role(user))  # what they may do inside Financials follows their role
    return any(path.startswith(prefix) for page in pages for prefix in PAGE_API.get(page, ()))


def allowed_kpis(user) -> set[str] | None:
    """None = every number; otherwise the numbers shown on this user's pages."""
    pages = allowed_pages(user)
    if pages is None:
        return None
    from insights.page_kpis import page_kpis

    return set().union(*(page_kpis(page) for page in pages)) if pages else set()


def allowed_drills(user) -> set[str] | None:
    """None = every record list; otherwise the lists behind the numbers on this user's pages."""
    pages = allowed_pages(user)
    if pages is None:
        return None
    from insights.page_kpis import page_drills

    return set().union(*(page_drills(page) for page in pages)) if pages else set()
