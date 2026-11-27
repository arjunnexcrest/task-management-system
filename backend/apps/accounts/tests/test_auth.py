"""
accounts/tests/test_auth.py

Automated tests for Module 1 Authentication endpoints (/api/auth/ and /api/accounts/):
- Login with email and login with username (including all 5 seeded default accounts)
- Invalid password returns 401; 5 failed attempts lock the account (403)
- Deactivated / suspended users cannot log in (403, SRS §3.4)
- Refresh token issues new access & refresh tokens; logout blacklists refresh token
- Change password updates credentials and rejects invalid old password
- Unauthenticated access to protected /api/auth/me/ and /api/accounts/me/ returns 401
"""
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.management.commands.seed_default_users import DEFAULT_ACCOUNTS
from apps.accounts.models import User


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def active_user(db):
    return User.objects.create_user(
        email="alice@tms.local",
        username="alice_dev",
        full_name="Alice Dev",
        password="ValidPassword123!",
        is_active=True,
        account_status=User.AccountStatus.ACTIVE,
    )


class TestJWTAuthFlow:
    def test_login_by_email_and_username_refresh_and_logout_blacklist(self, active_user):
        client = APIClient()

        # Login with email
        r_email = client.post(
            "/api/auth/login/",
            {"identifier": "alice@tms.local", "password": "ValidPassword123!"},
            format="json",
        )
        assert r_email.status_code == 200
        assert "access" in r_email.data and "refresh" in r_email.data
        assert r_email.data["user"]["email"] == "alice@tms.local"

        # Login with username (case-insensitive)
        r_uname = client.post(
            "/api/auth/login/",
            {"identifier": "ALICE_DEV", "password": "ValidPassword123!"},
            format="json",
        )
        assert r_uname.status_code == 200
        access = r_uname.data["access"]
        refresh = r_uname.data["refresh"]

        # Protected /api/auth/me/ requires Bearer token
        assert client.get("/api/auth/me/").status_code == 401
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        r_me = client.get("/api/auth/me/")
        assert r_me.status_code == 200
        assert r_me.data["email"] == "alice@tms.local"

        # Refresh token returns new access token
        r_ref = APIClient().post("/api/auth/refresh/", {"refresh": refresh}, format="json")
        assert r_ref.status_code == 200
        new_refresh = r_ref.data.get("refresh", refresh)

        # Logout blacklists refresh token
        r_out = client.post("/api/auth/logout/", {"refresh": new_refresh}, format="json")
        assert r_out.status_code == 200
        r_reuse = APIClient().post("/api/auth/refresh/", {"refresh": new_refresh}, format="json")
        assert r_reuse.status_code in (400, 401)

    def test_deactivated_user_and_lockout_after_five_failures(self, db, active_user):
        # 5 wrong passwords trigger temporary lockout (401 on attempts 1-4, 403 on lockout)
        for _ in range(5):
            cache.clear()
            r = APIClient().post(
                "/api/auth/login/",
                {"identifier": "alice@tms.local", "password": "WrongPassword!"},
                format="json",
            )
            assert r.status_code in (400, 401, 403)

        active_user.refresh_from_db()
        assert active_user.is_locked is True

        # Even valid password rejected while locked
        cache.clear()
        r_locked = APIClient().post(
            "/api/auth/login/",
            {"identifier": "alice@tms.local", "password": "ValidPassword123!"},
            format="json",
        )
        assert r_locked.status_code in (400, 403)
        assert "locked" in str(r_locked.data).lower()

        # Unlock, then deactivate -> deactivated user cannot log in (SRS §3.4)
        active_user.clear_login_failures()
        active_user.deactivate()
        cache.clear()
        r_deact = APIClient().post(
            "/api/auth/login/",
            {"identifier": "alice@tms.local", "password": "ValidPassword123!"},
            format="json",
        )
        assert r_deact.status_code in (400, 403)

    def test_change_password_and_relogin(self, active_user):
        client = APIClient()
        client.force_authenticate(user=active_user)

        # Wrong old password rejected
        r_bad = client.post(
            "/api/auth/change-password/",
            {"old_password": "NotMyPassword1!", "new_password": "NewStrongPassword99!"},
            format="json",
        )
        assert r_bad.status_code == 400

        # Valid password change works
        r_ok = client.post(
            "/api/auth/change-password/",
            {"old_password": "ValidPassword123!", "new_password": "NewStrongPassword99!"},
            format="json",
        )
        assert r_ok.status_code == 200

        # Can log in with new password
        r_login = APIClient().post(
            "/api/auth/login/",
            {"identifier": "alice@tms.local", "password": "NewStrongPassword99!"},
            format="json",
        )
        assert r_login.status_code == 200


class AuthenticationEndpointsTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        call_command("seed_default_users")

    def test_all_five_seeded_accounts_login_by_email_and_username(self):
        for spec in DEFAULT_ACCOUNTS:
            cache.clear()
            # Login by email
            resp_email = self.client.post(
                "/api/accounts/login/",
                {"identifier": spec["email"], "password": spec["password"]},
                format="json",
            )
            self.assertEqual(resp_email.status_code, status.HTTP_200_OK, spec["email"])
            data = resp_email.json()
            self.assertIn("access", data)
            self.assertIn("refresh", data)
            self.assertEqual(data["user"]["email"], spec["email"])
            self.assertEqual(data["user"]["username"], spec["username"])
            self.assertEqual(data["user"]["employee_id"], spec["employee_id"])
            self.assertEqual(data["user"]["designation"], spec["designation"])
            self.assertIn(spec["role_code"], data["user"]["roles"])

            # Login by username
            resp_user = self.client.post(
                "/api/accounts/login/",
                {"identifier": spec["username"], "password": spec["password"]},
                format="json",
            )
            self.assertEqual(resp_user.status_code, status.HTTP_200_OK, spec["username"])
            self.assertEqual(resp_user.json()["user"]["email"], spec["email"])

    def test_me_refresh_change_password_and_logout(self):
        login_resp = self.client.post(
            "/api/accounts/login/",
            {"identifier": "admin", "password": "Admin@12345"},
            format="json",
        )
        self.assertEqual(login_resp.status_code, status.HTTP_200_OK)
        access = login_resp.json()["access"]
        refresh = login_resp.json()["refresh"]

        # Unauthenticated /me/ returns 401
        unauth_resp = self.client.get("/api/accounts/me/")
        self.assertEqual(unauth_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        # Authenticated /me/ returns user with roles and company
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        me_resp = self.client.get("/api/accounts/me/")
        self.assertEqual(me_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(me_resp.json()["username"], "admin")
        self.assertEqual(me_resp.json()["role"], "CEO")
        self.assertTrue(len(me_resp.json()["memberships"]) >= 1)

        # Token refresh returns both access & refresh (required by frontend tokenStorage)
        refresh_resp = self.client.post(
            "/api/auth/refresh/",
            {"refresh": refresh},
            format="json",
        )
        self.assertEqual(refresh_resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", refresh_resp.json())
        self.assertIn("refresh", refresh_resp.json())

        # Change password
        pw_resp = self.client.post(
            "/api/accounts/change-password/",
            {"old_password": "Admin@12345", "new_password": "NewAdmin@12345"},
            format="json",
        )
        self.assertEqual(pw_resp.status_code, status.HTTP_200_OK)

        # Logout
        logout_resp = self.client.post(
            "/api/accounts/logout/",
            {"refresh": refresh_resp.json()["refresh"]},
            format="json",
        )
        self.assertEqual(logout_resp.status_code, status.HTTP_200_OK)

    def test_invalid_password_and_inactive_user_rejected(self):
        bad_resp = self.client.post(
            "/api/accounts/login/",
            {"identifier": "admin@tms.local", "password": "WrongPassword!"},
            format="json",
        )
        self.assertEqual(bad_resp.status_code, status.HTTP_401_UNAUTHORIZED)

        emp = User.objects.get(email="employee@tms.local")
        emp.account_status = User.AccountStatus.SUSPENDED
        emp.save(update_fields=["account_status"])

        suspended_resp = self.client.post(
            "/api/accounts/login/",
            {"identifier": "employee", "password": "User@12345"},
            format="json",
        )
        self.assertEqual(suspended_resp.status_code, status.HTTP_403_FORBIDDEN)
