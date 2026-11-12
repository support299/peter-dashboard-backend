"""Users page (Peter, Nov 2026): admins add people and choose what they can see."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import RefreshToken, UserAccess
from financials.access import finance_role

PASSWORD = "Tempo-Clean-2026!"


class UsersApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.peter = User.objects.create_superuser("peter@x.ca", "peter@x.ca", "pw")
        self.cassidy = User.objects.create_user("cassidy@x.ca", "cassidy@x.ca", "pw", first_name="Cassidy")
        UserAccess.objects.create(user=self.cassidy, pages=["master:manager"])

    def send(self, method, path, body=None):
        return getattr(self.client, method)(path, data=json.dumps(body or {}), content_type="application/json")

    def create(self, **overrides):
        body = {"email": "new@x.ca", "first_name": "New", "last_name": "Person", "password": PASSWORD, "access": "pages", "pages": ["master:revenue", "jobber"], "finance_role": ""}
        body.update(overrides)
        return self.send("post", "/api/accounts/users/", body)

    def test_only_admins(self):
        User = get_user_model()
        office = User.objects.create_user("office@x.ca", "office@x.ca", "pw", is_staff=True)
        for user in (self.cassidy, office, User.objects.create_user("plain@x.ca", "plain@x.ca", "pw")):
            self.client.force_login(user)
            self.assertEqual(self.client.get("/api/accounts/users/").status_code, 403, user.username)
            self.assertEqual(self.create().status_code, 403, user.username)
            self.assertEqual(self.send("patch", f"/api/accounts/users/{self.cassidy.pk}/", {"access": "everything"}).status_code, 403, user.username)
        self.client.logout()
        self.assertEqual(self.client.get("/api/accounts/users/").status_code, 401)
        self.assertEqual(UserAccess.objects.get(user=self.cassidy).pages, ["master:manager"])  # nobody changed it

    def test_list(self):
        self.client.force_login(self.peter)
        data = self.client.get("/api/accounts/users/").json()
        rows = {row["email"]: row for row in data["users"]}
        self.assertEqual((rows["peter@x.ca"]["is_admin"], rows["peter@x.ca"]["access"]), (True, "everything"))
        self.assertEqual((rows["cassidy@x.ca"]["access"], rows["cassidy@x.ca"]["pages"]), ("pages", ["master:manager"]))
        self.assertIn("master:overview", [p["key"] for p in data["options"]["pages"]])
        self.assertGreaterEqual(len(data["suggested_password"]), 12)
        self.assertNotIn("password", json.dumps(data["users"]))  # never sends passwords back

    def test_create_and_sign_in(self):
        self.client.force_login(self.peter)
        response = self.create(email=" New@X.ca ", finance_role="entry")
        self.assertEqual(response.status_code, 201, response.content)
        user = get_user_model().objects.get(email="new@x.ca")
        self.assertEqual((user.username, user.first_name, user.is_staff, user.is_superuser), ("new@x.ca", "New", False, False))
        self.assertEqual((UserAccess.objects.get(user=user).pages, finance_role(user)), (["master:revenue", "jobber"], "entry"))
        self.client.logout()
        login = self.send("post", "/api/accounts/login/", {"email": "new@x.ca", "password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        me = self.client.get("/api/accounts/me/").json()["user"]
        self.assertEqual(me["pages"], ["master:revenue", "jobber"])
        self.assertEqual(self.client.get("/api/insights/revenue/").status_code != 403, True)
        self.assertEqual(self.client.get("/api/insights/overview/").status_code, 403)

    def test_create_validation(self):
        self.client.force_login(self.peter)
        for overrides, message in (
            ({"email": "not-an-email"}, "valid email"),
            ({"email": "CASSIDY@x.ca"}, "already an account"),
            ({"first_name": " "}, "first name"),
            ({"access": "pages", "pages": []}, "at least one page"),
            ({"pages": ["master:rules"]}, "Unknown page"),
            ({"access": "admin"}, "Choose what this person can see"),
            ({"finance_role": "owner"}, "Unknown Financials"),
            ({"password": "123"}, "too short"),
            ({"password": ""}, "temporary password"),
        ):
            response = self.create(**overrides)
            self.assertEqual(response.status_code, 400, overrides)
            self.assertIn(message.lower(), response.json()["error"].lower(), overrides)
        self.assertFalse(get_user_model().objects.filter(email="new@x.ca").exists())

    def test_everything_access_has_no_limit(self):
        self.client.force_login(self.peter)
        self.assertEqual(self.create(access="everything", pages=["master:mrr"]).status_code, 201)
        user = get_user_model().objects.get(email="new@x.ca")
        self.assertFalse(UserAccess.objects.filter(user=user).exists())
        self.assertFalse(user.is_staff or user.is_superuser)  # "everything" isn't admin: no user management

    def test_edit_access_password_and_disable(self):
        self.client.force_login(self.peter)
        url = f"/api/accounts/users/{self.cassidy.pk}/"
        response = self.send("patch", url, {"access": "pages", "pages": ["master:manager", "master:quality"], "finance_role": "full"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((UserAccess.objects.get(user=self.cassidy).pages, finance_role(self.cassidy)), (["master:manager", "master:quality"], "full"))
        # Only the Financials role: pages stay as they were
        self.send("patch", url, {"finance_role": ""})
        self.assertEqual((UserAccess.objects.get(user=self.cassidy).pages, finance_role(get_user_model().objects.get(pk=self.cassidy.pk))), (["master:manager", "master:quality"], ""))
        # New password signs her out everywhere
        RefreshToken.issue(self.cassidy, days=7)
        self.assertEqual(self.send("patch", url, {"password": PASSWORD}).status_code, 200)
        self.assertFalse(RefreshToken.objects.filter(user=self.cassidy, revoked_at__isnull=True).exists())
        self.client.logout()
        self.assertEqual(self.send("post", "/api/accounts/login/", {"email": "cassidy@x.ca", "password": PASSWORD}).status_code, 200)
        # Disabled: can't sign in, and an open session stops working at once
        self.client.cookies.clear()  # drop Cassidy's sign-in cookie before acting as Peter
        self.client.force_login(self.peter)
        self.assertEqual(self.send("patch", url, {"is_active": False}).status_code, 200)
        self.client.logout()
        self.assertEqual(self.send("post", "/api/accounts/login/", {"email": "cassidy@x.ca", "password": PASSWORD}).status_code, 401)
        self.client.force_login(get_user_model().objects.get(pk=self.cassidy.pk))
        self.assertEqual(self.client.get("/api/insights/manager/").status_code, 401)
        # Re-enabled
        self.client.cookies.clear()
        self.client.force_login(self.peter)
        self.assertEqual(self.send("patch", url, {"is_active": True}).status_code, 200)
        self.assertTrue(get_user_model().objects.get(pk=self.cassidy.pk).is_active)

    def test_admins_and_yourself_cant_be_changed_here(self):
        other_admin = get_user_model().objects.create_superuser("boss@x.ca", "boss@x.ca", "pw")
        self.client.force_login(self.peter)
        self.assertEqual(self.send("patch", f"/api/accounts/users/{other_admin.pk}/", {"is_active": False}).status_code, 400)
        self.assertEqual(self.send("patch", f"/api/accounts/users/{self.peter.pk}/", {"is_active": False}).status_code, 400)
        self.assertTrue(get_user_model().objects.get(pk=other_admin.pk).is_active)
        self.assertEqual(self.send("patch", "/api/accounts/users/99999/", {"is_active": False}).status_code, 404)

    def test_staff_pages_cant_be_limited(self):
        office = get_user_model().objects.create_user("office@x.ca", "office@x.ca", "pw", is_staff=True)
        self.client.force_login(self.peter)
        response = self.send("patch", f"/api/accounts/users/{office.pk}/", {"access": "pages", "pages": ["master:manager"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("always see everything", response.json()["error"])
        self.assertFalse(UserAccess.objects.filter(user=office).exists())
        self.assertEqual(self.send("patch", f"/api/accounts/users/{office.pk}/", {"finance_role": "full"}).status_code, 200)  # other changes still fine

    def test_only_json_changes(self):
        self.client.force_login(self.peter)
        form = self.client.post("/api/accounts/users/", {"email": "x@x.ca", "first_name": "X", "password": PASSWORD, "access": "everything"})
        self.assertEqual(form.status_code, 415)  # a plain form post, like one from another website
        self.assertFalse(get_user_model().objects.filter(email="x@x.ca").exists())
        self.assertEqual(self.send("patch", f"/api/accounts/users/{self.cassidy.pk}/", {"is_active": "false"}).status_code, 400)
        self.assertTrue(get_user_model().objects.get(pk=self.cassidy.pk).is_active)

    def test_role_change_keeps_an_unreadable_page_list(self):
        UserAccess.objects.filter(user=self.cassidy).update(pages=["master:managers"])  # an old typo
        self.client.force_login(self.peter)
        self.assertEqual(self.send("patch", f"/api/accounts/users/{self.cassidy.pk}/", {"finance_role": "entry"}).status_code, 200)
        self.assertEqual(UserAccess.objects.get(user=self.cassidy).pages, ["master:managers"])  # still locked down, untouched

    def test_bad_input_on_edit_changes_nothing(self):
        self.client.force_login(self.peter)
        url = f"/api/accounts/users/{self.cassidy.pk}/"
        response = self.send("patch", url, {"first_name": "Cass", "access": "pages", "pages": ["nope"]})
        self.assertEqual(response.status_code, 400)
        cassidy = get_user_model().objects.get(pk=self.cassidy.pk)
        self.assertEqual((cassidy.first_name, UserAccess.objects.get(user=cassidy).pages), ("Cassidy", ["master:manager"]))


class CustomPagesAccessTests(TestCase):
    """Someone given Revenue + the Jobber section only."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("mix@x.ca", "mix@x.ca", "pw")
        UserAccess.objects.create(user=self.user, pages=["master:revenue", "jobber"])
        self.client.force_login(self.user)

    def test_pages_sections_and_refresh(self):
        for path in ("/api/insights/revenue/", "/api/operations/dashboard/", "/api/jobber/status/"):
            self.assertNotEqual(self.client.get(path).status_code, 403, path)
        for path in ("/api/insights/overview/", "/api/insights/manager/", "/api/insights/mrr/", "/api/admin-internal/status/", "/api/pricing-calculator/status/", "/api/financials/meta/"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        for path in ("/api/jobber/sync/", "/api/jobber/disconnect/", "/api/admin-internal/sync/"):
            self.assertEqual(self.client.post(path).status_code, 403, path)  # refreshing data stays with full users

    def test_lists_and_trends_follow_the_pages(self):
        from accounts.access import allowed_drills, allowed_kpis

        self.assertIn("revenue", allowed_drills(self.user))
        self.assertNotIn("complaints", allowed_drills(self.user))
        self.assertEqual(self.client.get("/api/insights/drill/?metric=complaints").status_code, 403)
        self.assertEqual(self.client.get("/api/insights/drill/?metric=mrr_plans").status_code, 403)
        self.assertIn("revenue", allowed_kpis(self.user))
        self.assertNotIn("mrr", allowed_kpis(self.user))

    def test_financials_follow_the_role(self):
        from financials.models import FinanceAccess

        self.assertEqual(self.client.get("/api/financials/meta/").status_code, 403)
        FinanceAccess.objects.create(user=self.user, role="full")
        self.assertNotEqual(self.client.get("/api/financials/meta/").status_code, 403)
