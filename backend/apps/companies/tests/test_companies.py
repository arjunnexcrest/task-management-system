"""
tests/test_companies.py

Tests for Company CRUD (/api/companies/) and User Management (/api/users/):
- CEO can create, view, update, and soft-delete a company
- Non-CEO gets 403 on company endpoints
- No token gets 401 on company and user endpoints
- Duplicate company name is rejected (400)
- Duplicate user email is rejected (400)
- Only CEO can create users and change roles (non-CEO gets 403)
- Password is write-only and hashed
- Data isolation: CEO sees everything; other users see only users from their own company
- List filters on /api/users/: company, role, is_active
- Activate / deactivate user actions and soft-deactivate on DELETE
- GET /api/users/me/ returns the logged-in user's profile
"""
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.rbac.roles import SystemRole

COMPANIES_URL = "/api/companies/"
USERS_URL = "/api/users/"
USERS_ME_URL = "/api/users/me/"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def company_a(db):
    return Company.objects.create(
        name="Alpha Corp",
        code="ALPHA",
        email="info@alpha.com",
        phone="+1-555-0101",
        address="100 Alpha Way",
        is_active=True,
    )


@pytest.fixture
def company_b(db):
    return Company.objects.create(
        name="Beta Ltd",
        code="BETA",
        email="contact@beta.com",
        phone="+1-555-0202",
        address="200 Beta Blvd",
        is_active=True,
    )


@pytest.fixture
def ceo_user(db, company_a):
    user = User.objects.create_user(
        email="ceo@alpha.com",
        username="ceo_alpha",
        full_name="CEO Alpha",
        password="CeoPassword123!",
        is_staff=True,
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.CEO,
        is_active=True,
    )
    return user


@pytest.fixture
def emp_company_a(db, company_a):
    user = User.objects.create_user(
        email="alice@alpha.com",
        username="alice_a",
        full_name="Alice Alpha",
        password="EmpPassword123!",
        is_staff=False,
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.EMPLOYEE,
        is_active=True,
    )
    return user


@pytest.fixture
def pm_company_b(db, company_b):
    user = User.objects.create_user(
        email="bob@beta.com",
        username="bob_b",
        full_name="Bob Beta",
        password="EmpPassword123!",
        is_staff=False,
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_b,
        role=SystemRole.PROJECT_MANAGER,
        is_active=True,
    )
    return user


def auth_client(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


# ---------------------------------------------------------------------------
# 1. Company CRUD (/api/companies/)
# ---------------------------------------------------------------------------
class TestCompanyAPI:
    def test_no_token_gets_401_on_companies(self, db, client):
        r_list = client.get(COMPANIES_URL)
        r_post = client.post(COMPANIES_URL, {"name": "Anon Co"}, format="json")
        assert r_list.status_code == 401
        assert r_post.status_code == 401

    def test_ceo_can_create_company(self, db, ceo_user):
        c = auth_client(ceo_user)
        r = c.post(
            COMPANIES_URL,
            {
                "name": "Gamma Innovations",
                "email": "hello@gamma.io",
                "phone": "+1-555-0303",
                "address": "300 Gamma Street",
            },
            format="json",
        )
        assert r.status_code == 201
        assert r.data["name"] == "Gamma Innovations"
        assert r.data["email"] == "hello@gamma.io"
        assert r.data["phone"] == "+1-555-0303"
        assert r.data["address"] == "300 Gamma Street"
        assert r.data["is_active"] is True
        assert "created_at" in r.data
        assert "updated_at" in r.data

    def test_duplicate_company_name_is_rejected(self, db, ceo_user, company_a):
        c = auth_client(ceo_user)
        r = c.post(
            COMPANIES_URL,
            {"name": "Alpha Corp", "email": "dup@alpha.com"},
            format="json",
        )
        assert r.status_code == 400

    def test_non_ceo_gets_403_on_companies(self, db, emp_company_a, company_a):
        c = auth_client(emp_company_a)
        assert c.get(COMPANIES_URL).status_code == 403
        assert c.get(f"{COMPANIES_URL}{company_a.id}/").status_code == 403
        assert c.post(COMPANIES_URL, {"name": "Blocked Co"}, format="json").status_code == 403
        assert c.patch(f"{COMPANIES_URL}{company_a.id}/", {"name": "Hacked"}, format="json").status_code == 403
        assert c.delete(f"{COMPANIES_URL}{company_a.id}/").status_code == 403

    def test_company_soft_delete_sets_is_active_false_and_keeps_row(self, db, ceo_user, company_b):
        c = auth_client(ceo_user)
        r = c.delete(f"{COMPANIES_URL}{company_b.id}/")
        assert r.status_code == 204

        company_b.refresh_from_db()
        assert company_b.is_active is False
        assert Company.objects.filter(pk=company_b.id).exists() is True

    def test_company_search_by_name_and_pagination(self, db, ceo_user, company_a, company_b):
        c = auth_client(ceo_user)
        r = c.get(f"{COMPANIES_URL}?search=Alpha")
        assert r.status_code == 200
        assert "count" in r.data
        assert "results" in r.data
        assert r.data["count"] == 1
        assert r.data["results"][0]["name"] == "Alpha Corp"


# ---------------------------------------------------------------------------
# 2. User Management APIs (/api/users/)
# ---------------------------------------------------------------------------
class TestUserManagementAPI:
    def test_no_token_gets_401_on_users(self, db, client):
        assert client.get(USERS_URL).status_code == 401
        assert client.get(USERS_ME_URL).status_code == 401
        assert client.post(USERS_URL, {}, format="json").status_code == 401

    def test_ceo_can_create_user_with_hashed_write_only_password(self, db, ceo_user, company_a):
        c = auth_client(ceo_user)
        r = c.post(
            USERS_URL,
            {
                "email": "newhire@alpha.com",
                "username": "newhire",
                "full_name": "New Hire",
                "password": "StrongPassword123!",
                "company": str(company_a.id),
                "role": User.RoleChoices.DEPARTMENT_HEAD,
            },
            format="json",
        )
        assert r.status_code == 201
        assert "password" not in r.data
        assert str(r.data["company"]) == str(company_a.id)
        assert r.data["role"] == User.RoleChoices.DEPARTMENT_HEAD

        created = User.objects.get(email="newhire@alpha.com")
        assert created.password != "StrongPassword123!"
        assert created.check_password("StrongPassword123!") is True

    def test_duplicate_email_is_rejected(self, db, ceo_user, emp_company_a, company_a):
        c = auth_client(ceo_user)
        r = c.post(
            USERS_URL,
            {
                "email": "ALICE@alpha.com",
                "full_name": "Duplicate Alice",
                "password": "StrongPassword123!",
                "company": str(company_a.id),
                "role": User.RoleChoices.EMPLOYEE,
            },
            format="json",
        )
        assert r.status_code == 400

    def test_non_ceo_cannot_create_user_or_change_role(self, db, emp_company_a, company_a):
        c = auth_client(emp_company_a)
        r_create = c.post(
            USERS_URL,
            {
                "email": "blocked@alpha.com",
                "full_name": "Blocked User",
                "password": "StrongPassword123!",
                "company": str(company_a.id),
                "role": User.RoleChoices.EMPLOYEE,
            },
            format="json",
        )
        assert r_create.status_code == 403

        r_role = c.patch(
            f"{USERS_URL}{emp_company_a.id}/",
            {"role": User.RoleChoices.CEO},
            format="json",
        )
        assert r_role.status_code == 403

    def test_user_cannot_see_another_companys_users_while_ceo_sees_all(
        self, db, emp_company_a, ceo_user, pm_company_b
    ):
        # Non-CEO in Company A only sees Company A users
        c_emp = auth_client(emp_company_a)
        r_emp = c_emp.get(USERS_URL)
        assert r_emp.status_code == 200
        emp_emails = [u["email"] for u in r_emp.data["results"]]
        assert "alice@alpha.com" in emp_emails
        assert "ceo@alpha.com" in emp_emails
        assert "bob@beta.com" not in emp_emails

        # Direct detail lookup of Company B user by Company A user returns 404
        assert c_emp.get(f"{USERS_URL}{pm_company_b.id}/").status_code == 404

        # CEO sees users across all companies
        c_ceo = auth_client(ceo_user)
        r_ceo = c_ceo.get(USERS_URL)
        assert r_ceo.status_code == 200
        ceo_emails = [u["email"] for u in r_ceo.data["results"]]
        assert "alice@alpha.com" in ceo_emails
        assert "bob@beta.com" in ceo_emails

    def test_filter_users_by_company_role_and_is_active(
        self, db, ceo_user, emp_company_a, pm_company_b, company_a, company_b
    ):
        c = auth_client(ceo_user)

        r_comp = c.get(f"{USERS_URL}?company={company_b.id}")
        assert r_comp.status_code == 200
        assert [u["email"] for u in r_comp.data["results"]] == ["bob@beta.com"]

        r_role = c.get(f"{USERS_URL}?role={User.RoleChoices.PROJECT_MANAGER}")
        assert r_role.status_code == 200
        assert [u["email"] for u in r_role.data["results"]] == ["bob@beta.com"]

        emp_company_a.deactivate()
        r_inactive = c.get(f"{USERS_URL}?is_active=false")
        assert r_inactive.status_code == 200
        assert "alice@alpha.com" in [u["email"] for u in r_inactive.data["results"]]

    def test_activate_and_deactivate_user_actions(self, db, ceo_user, emp_company_a):
        c = auth_client(ceo_user)

        r_deact = c.post(f"{USERS_URL}{emp_company_a.id}/deactivate/")
        assert r_deact.status_code == 200
        emp_company_a.refresh_from_db()
        assert emp_company_a.is_active is False

        r_act = c.post(f"{USERS_URL}{emp_company_a.id}/activate/")
        assert r_act.status_code == 200
        emp_company_a.refresh_from_db()
        assert emp_company_a.is_active is True

    def test_users_me_endpoint_returns_logged_in_profile(self, db, emp_company_a):
        c = auth_client(emp_company_a)
        r_get = c.get(USERS_ME_URL)
        assert r_get.status_code == 200
        assert r_get.data["email"] == "alice@alpha.com"
        assert r_get.data["role"] == User.RoleChoices.EMPLOYEE
