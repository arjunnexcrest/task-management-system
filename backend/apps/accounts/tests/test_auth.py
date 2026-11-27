"""
accounts/tests/test_auth.py

Automated tests for Module 1 Authentication endpoints (/api/auth/ and /api/accounts/):
- Login with email and login with username
- Invalid password returns generic 400 error; 5 failed attempts lock the account
- Deactivated users cannot log in (SRS §3.4)
- Refresh token issues new access token; logout blacklists refresh token
- Change password updates credentials and rejects invalid old password
- Unauthenticated access to protected /api/auth/me/ returns 401
"""
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

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
        # 5 wrong passwords trigger temporary lockout
        for _ in range(5):
            cache.clear()
            r = APIClient().post(
                "/api/auth/login/",
                {"identifier": "alice@tms.local", "password": "WrongPassword!"},
                format="json",
            )
            assert r.status_code == 400

        active_user.refresh_from_db()
        assert active_user.is_locked is True

        # Even valid password rejected while locked
        cache.clear()
        r_locked = APIClient().post(
            "/api/auth/login/",
            {"identifier": "alice@tms.local", "password": "ValidPassword123!"},
            format="json",
        )
        assert r_locked.status_code == 400
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
        assert r_deact.status_code == 400

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
