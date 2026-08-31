"""Smoke checks for API auth gate + HttpOnly cookie session."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from accounts.cookies import ACCESS_COOKIE, REFRESH_COOKIE
from accounts.models import RefreshToken


@override_settings(
    ROOT_URLCONF="config.urls",
    ALLOWED_HOSTS=["*"],
    JWT_COOKIE_SECURE=False,
)
class ApiAuthGateTests(TestCase):
    def setUp(self):
        self.client = Client()
        User = get_user_model()
        self.user = User.objects.create_user(
            username="dash@example.com",
            email="dash@example.com",
            password="TestPass123!",
        )
        self.staff = User.objects.create_user(
            username="staff@example.com",
            email="staff@example.com",
            password="TestPass123!",
            is_staff=True,
        )

    def _login(self, email="dash@example.com", password="TestPass123!"):
        return self.client.post(
            "/api/accounts/login/",
            data=f'{{"email":"{email}","password":"{password}"}}',
            content_type="application/json",
        )

    def test_protected_endpoints_reject_anonymous(self):
        paths = [
            "/api/jobber/status/",
            "/api/operations/dashboard/",
            "/api/admin-internal/status/",
            "/api/pricing-calculator/status/",
            "/api/dashboards/",
            "/api/accounts/me/",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401, path)
                self.assertFalse(response.json().get("ok", True))

    def test_public_auth_endpoints_accept_post_without_token(self):
        response = self.client.post(
            "/api/accounts/login/",
            data='{"email":"x","password":"y"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("Invalid", response.json().get("error", ""))

    def test_staff_only_rejects_non_staff(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/operations/celery/status/")
        self.assertEqual(response.status_code, 403)

    def test_staff_only_allows_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get("/api/operations/celery/status/")
        self.assertNotEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 403)

    def test_authenticated_user_can_read_status(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/jobber/status/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok"))

    def test_login_sets_httponly_cookies_without_token_body(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("ok"))
        self.assertNotIn("access_token", body)
        self.assertNotIn("refresh_token", body)
        self.assertIn(ACCESS_COOKIE, response.cookies)
        self.assertIn(REFRESH_COOKIE, response.cookies)
        self.assertTrue(response.cookies[ACCESS_COOKIE]["httponly"])
        self.assertTrue(response.cookies[REFRESH_COOKIE]["httponly"])

    def test_cookie_session_can_call_me(self):
        self._login()
        response = self.client.get("/api/accounts/me/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["email"], "dash@example.com")

    def test_refresh_rotates_and_revokes_old(self):
        login_resp = self._login()
        old_refresh = login_resp.cookies[REFRESH_COOKIE].value
        old_hash_count = RefreshToken.objects.filter(user=self.user).count()
        self.assertEqual(old_hash_count, 1)

        refresh_resp = self.client.post(
            "/api/accounts/refresh/",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(refresh_resp.status_code, 200)
        self.assertNotIn("refresh_token", refresh_resp.json())
        new_refresh = refresh_resp.cookies[REFRESH_COOKIE].value
        self.assertNotEqual(old_refresh, new_refresh)

        old_row = RefreshToken.objects.get(user=self.user, revoked_at__isnull=False)
        self.assertIsNotNone(old_row.revoked_at)

        # Replay old refresh → reuse detection, all sessions revoked
        self.client.cookies[REFRESH_COOKIE] = old_refresh
        reuse = self.client.post(
            "/api/accounts/refresh/",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(reuse.status_code, 401)
        self.assertFalse(RefreshToken.objects.filter(user=self.user, revoked_at__isnull=True).exists())
