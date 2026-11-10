"""
Which pages a user may open (Peter, Nov 2026: "limit Cassidy to only the manager view").

A user with no UserAccess row, or an empty page list, keeps full access, as before. Staff and
superusers always have full access. A user with pages listed may only open those pages: the API
refuses everything else, so typing another address doesn't help. A list that's filled in but
unreadable (a typo, plain text) locks the user out rather than letting them see everything.

Pages are "<section>:<page>", e.g. "master:manager". Peter's future Users page will edit the same list.
"""

from __future__ import annotations

PAGES = {
    "master:manager": "Manager overview",
}

# API paths each page needs.
PAGE_API = {
    "master:manager": ("/api/insights/manager/", "/api/insights/drill/"),
}

# Needed by every signed-in user: who am I, change password, sign out, and the "Updated 2h ago" line.
ALWAYS_ALLOWED = ("/api/accounts/", "/api/jobber/status/")


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
    return any(path.startswith(prefix) for page in pages for prefix in PAGE_API.get(page, ()))
