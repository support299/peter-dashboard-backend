"""Page access (Peter, Nov 2026: Cassidy sees the Manager overview only). Tries to get around it."""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLResolver, get_resolver

from accounts.access import ALWAYS_ALLOWED, PAGE_API, allowed_pages
from accounts.models import UserAccess

ALLOWED_PREFIXES = ALWAYS_ALLOWED + PAGE_API["master:manager"]
# Public on purpose (login, OAuth callback, signed webhooks): reachable without signing in at all.
PUBLIC = ("/api/accounts/login/", "/api/accounts/refresh/", "/api/accounts/forgot-password/", "/api/accounts/reset-password/",
          "/api/accounts/logout/", "/api/jobber/webhooks/", "/api/jobber/callback/")


def api_paths():
    """Every /api/ route the app has, with its parameters filled in."""

    def walk(patterns, prefix):
        for pattern in patterns:
            route = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                yield from walk(pattern.url_patterns, route)
            else:
                yield route

    for route in walk(get_resolver().url_patterns, "/"):
        if not route.startswith("/api/"):
            continue
        route = re.sub(r"<int:[^>]+>", "1", route)
        route = re.sub(r"<(?:str|slug|uuid|path):[^>]+>|<[^>]+>", "x", route)
        yield route.replace("^", "").replace("$", "")


class PageAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.cassidy = User.objects.create_user("cassidy", "c@x.ca", "pw")
        UserAccess.objects.create(user=self.cassidy, pages=["master:manager"])

    def test_every_other_api_route_is_refused(self):
        self.client.force_login(self.cassidy)
        paths = sorted(set(api_paths()))
        self.assertGreater(len(paths), 80)  # the walk really found the app's routes
        checked = 0
        for path in paths:
            if path.startswith(ALLOWED_PREFIXES) or path.startswith(PUBLIC):
                continue
            for method in ("get", "post", "patch", "delete"):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 403, f"{method.upper()} {path} -> {response.status_code}")
                checked += 1
        self.assertGreater(checked, 300)

    def test_tricky_addresses_dont_reach_other_pages(self):
        self.client.force_login(self.cassidy)
        for path in (
            "/api/insights/manager/../overview/",
            "/api/insights/manager//../overview/",
            "/api/insights/manager/%2e%2e/overview/",
            "/api/insights/manager%2f..%2foverview/",
            "/api/insights/overview",
            "/API/insights/overview/",
            "/api/insights//overview/",
            "/api/insights/drillx/",
            "/api/jobber/status/../sync/",
        ):
            for method in ("get", "post"):
                response = getattr(self.client, method)(path)
                self.assertIn(response.status_code, (403, 404, 405), f"{method.upper()} {path} -> {response.status_code}")

    def test_forged_script_prefix_is_refused(self):
        # A proxy that passes SCRIPT_NAME through would make request.path look like a manager address
        # while the router still sees /api/insights/overview/. The check must use what the router uses.
        self.client.force_login(self.cassidy)
        response = self.client.get("/api/insights/overview/", SCRIPT_NAME="/api/insights/manager")
        self.assertEqual(response.status_code, 403)

    def test_drill_lists_limited_to_her_page(self):
        self.client.force_login(self.cassidy)
        for metric in ("expenses_cogs", "gross_profit", "payments", "receivables", "mrr_plans", "", "../revenue"):
            self.assertEqual(self.client.get(f"/api/insights/drill/?metric={metric}").status_code, 403, metric)

    def test_typos_lock_down_instead_of_opening_up(self):
        for pages, expected in (
            (["master:managers"], []),  # typo: no page, not everything
            ("master:manager", ["master:manager"]),  # plain text still understood
            ({"page": "master:manager"}, []),  # unreadable
            (["master:manager", 7, None], ["master:manager"]),
            ([], None),  # explicitly empty = everything
        ):
            UserAccess.objects.filter(user=self.cassidy).update(pages=pages)
            user = get_user_model().objects.get(pk=self.cassidy.pk)
            self.assertEqual(allowed_pages(user), expected, pages)
        UserAccess.objects.filter(user=self.cassidy).update(pages=["master:managers"])
        self.client.force_login(self.cassidy)
        self.assertEqual(self.client.get("/api/insights/manager/").status_code, 403)
        self.assertEqual(self.client.get("/api/accounts/me/").json()["user"]["pages"], [])

    def test_others_keep_full_access(self):
        User = get_user_model()
        no_row = User.objects.create_user("peter", "p@x.ca", "pw")
        staff = User.objects.create_user("office", "o@x.ca", "pw", is_staff=True)
        UserAccess.objects.create(user=staff, pages=["master:manager"])  # staff always see everything
        boss = User.objects.create_superuser("boss", "b@x.ca", "pw")
        UserAccess.objects.create(user=boss, pages=["master:manager"])
        for user in (no_row, staff, boss):
            self.client.force_login(user)
            self.assertIsNone(self.client.get("/api/accounts/me/").json()["user"]["pages"], user.username)
            self.assertNotEqual(self.client.get("/api/insights/overview/").status_code, 403, user.username)
            self.assertNotEqual(self.client.get("/api/admin-internal/status/").status_code, 403, user.username)

    def test_limit_applies_immediately(self):
        self.client.force_login(self.cassidy)
        self.assertEqual(self.client.get("/api/insights/overview/").status_code, 403)
        UserAccess.objects.filter(user=self.cassidy).update(pages=[])
        self.assertNotEqual(self.client.get("/api/insights/overview/").status_code, 403)  # no new login needed
        UserAccess.objects.filter(user=self.cassidy).update(pages=["master:manager"])
        self.assertEqual(self.client.get("/api/insights/overview/").status_code, 403)

    def test_admin_site_stays_closed(self):
        self.client.force_login(self.cassidy)
        response = self.client.get("/admin/accounts/useraccess/")
        self.assertIn(response.status_code, (302, 403))  # sent to the admin login: she isn't staff
        self.assertNotEqual(self.client.post("/admin/accounts/useraccess/1/change/", {"pages": "[]"}).status_code, 200)
