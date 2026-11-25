from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.management.commands.seed_default_users import DEFAULT_ACCOUNTS


class AuthenticationEndpointsTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        call_command("seed_default_users")

    def test_all_five_seeded_accounts_login_by_email_and_username(self):
        for spec in DEFAULT_ACCOUNTS:
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
