"""
tests/test_memberships.py

Tests for Roles, CompanyMembership (/api/memberships/), Permission Foundation
(IsCEO, HasRole, IsCompanyMember, get_active_membership), and updated Company/User APIs:
- CEO adds a member to a company with a role
- Duplicate (user, company) membership is rejected (400)
- Non-member / non-CEO gets 403
- CEO changes a member's role and deactivates/reactivates a membership
- GET /api/users/me/ returns the user's memberships (company & role)
- Reusable HasRole(roles) and IsCompanyMember permissions work as expected
"""
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.models import User
from apps.common.permissions import HasRole, IsCEO, IsCompanyMember, get_active_membership
from apps.companies.models import Company, CompanyMembership
from apps.rbac.roles import SystemRole

MEMBERSHIPS_URL = "/api/memberships/"
COMPANIES_URL = "/api/companies/"
USERS_URL = "/api/users/"
USERS_ME_URL = "/api/users/me/"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def company_a(db):
    return Company.objects.create(
        name="Alpha Corp",
        code="ALPHA",
        email="info@alpha.com",
        is_active=True,
    )


@pytest.fixture
def company_b(db):
    return Company.objects.create(
        name="Beta Ltd",
        code="BETA",
        email="contact@beta.com",
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
def dept_head_a(db, company_a):
    user = User.objects.create_user(
        email="head@alpha.com",
        username="head_alpha",
        full_name="Dept Head Alpha",
        password="EmpPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.DEPARTMENT_HEAD,
        is_active=True,
    )
    return user


@pytest.fixture
def emp_b(db, company_b):
    user = User.objects.create_user(
        email="bob@beta.com",
        username="bob_beta",
        full_name="Bob Beta",
        password="EmpPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_b,
        role=SystemRole.EMPLOYEE,
        is_active=True,
    )
    return user


@pytest.fixture
def unassigned_user(db):
    return User.objects.create_user(
        email="newbie@example.com",
        username="newbie",
        full_name="Newbie User",
        password="EmpPassword123!",
        is_active=True,
    )


def auth_client(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TestCompanyMembershipAPI:
    def test_ceo_adds_member_with_role(self, db, ceo_user, company_a, unassigned_user):
        c = auth_client(ceo_user)
        r = c.post(
            MEMBERSHIPS_URL,
            {
                "user": str(unassigned_user.id),
                "company": str(company_a.id),
                "role": SystemRole.TEAM_LEADER,
            },
            format="json",
        )
        assert r.status_code == 201
        assert r.data["role"] == SystemRole.TEAM_LEADER
        assert r.data["is_active"] is True
        assert str(r.data["company"]) == str(company_a.id)
        assert str(r.data["user"]) == str(unassigned_user.id)

    def test_duplicate_membership_is_rejected(self, db, ceo_user, company_a, dept_head_a):
        c = auth_client(ceo_user)
        r = c.post(
            MEMBERSHIPS_URL,
            {
                "user": str(dept_head_a.id),
                "company": str(company_a.id),
                "role": SystemRole.EMPLOYEE,
            },
            format="json",
        )
        assert r.status_code == 400

    def test_non_member_and_non_ceo_get_403(self, db, dept_head_a, emp_b, company_a, company_b, unassigned_user):
        # Non-CEO cannot add a member
        c_head = auth_client(dept_head_a)
        r_add = c_head.post(
            MEMBERSHIPS_URL,
            {
                "user": str(unassigned_user.id),
                "company": str(company_a.id),
                "role": SystemRole.EMPLOYEE,
            },
            format="json",
        )
        assert r_add.status_code == 403

        # Non-member querying another company's memberships gets 403
        r_other_members = c_head.get(f"{MEMBERSHIPS_URL}?company={company_b.id}")
        assert r_other_members.status_code == 403

        # Non-member querying another company's users gets 403
        r_other_users = c_head.get(f"{USERS_URL}?company={company_b.id}")
        assert r_other_users.status_code == 403

        # Non-CEO gets 403 on /api/companies/
        assert c_head.get(COMPANIES_URL).status_code == 403

        # Unassigned non-member user gets 403 on /api/memberships/
        c_unassigned = auth_client(unassigned_user)
        assert c_unassigned.get(MEMBERSHIPS_URL).status_code == 403

    def test_ceo_changes_member_role_and_deactivates_membership(self, db, ceo_user, company_a, dept_head_a):
        c = auth_client(ceo_user)
        membership = CompanyMembership.objects.get(user=dept_head_a, company=company_a)

        # Change role
        r_patch = c.patch(
            f"{MEMBERSHIPS_URL}{membership.id}/",
            {"role": SystemRole.PROJECT_MANAGER},
            format="json",
        )
        assert r_patch.status_code == 200
        assert r_patch.data["role"] == SystemRole.PROJECT_MANAGER
        membership.refresh_from_db()
        assert membership.role == SystemRole.PROJECT_MANAGER

        # Deactivate membership
        r_deact = c.post(f"{MEMBERSHIPS_URL}{membership.id}/deactivate/")
        assert r_deact.status_code == 200
        membership.refresh_from_db()
        assert membership.is_active is False
        assert membership.status == CompanyMembership.Status.INACTIVE

        # Active membership helper returns None after deactivation
        assert get_active_membership(dept_head_a, company_id=company_a.id) is None

    def test_users_me_returns_memberships(self, db, dept_head_a, company_a):
        c = auth_client(dept_head_a)
        r = c.get(USERS_ME_URL)
        assert r.status_code == 200
        assert "memberships" in r.data
        assert len(r.data["memberships"]) == 1
        assert r.data["memberships"][0]["company"] == str(company_a.id)
        assert r.data["memberships"][0]["role"] == SystemRole.DEPARTMENT_HEAD


class TestPermissionFoundationHelpers:
    def test_has_role_and_is_company_member_permissions(self, db, ceo_user, dept_head_a, emp_b, company_a, company_b):
        factory = APIRequestFactory()

        # HasRole([DepartmentHead]) allows dept_head_a in company_a, denies emp_b
        perm_cls = HasRole([SystemRole.DEPARTMENT_HEAD])
        perm = perm_cls()

        req_a = factory.get(f"/?company={company_a.id}")
        req_a.query_params = {"company": str(company_a.id)}
        req_a.user = dept_head_a
        assert perm.has_permission(req_a, None) is True

        req_b = factory.get(f"/?company={company_b.id}")
        req_b.query_params = {"company": str(company_b.id)}
        req_b.user = emp_b
        assert perm.has_permission(req_b, None) is False

        # IsCompanyMember allows active member of company_a, denies member of company_b
        member_perm = IsCompanyMember()
        assert member_perm.has_permission(req_a, None) is True

        req_cross = factory.get(f"/?company={company_a.id}")
        req_cross.query_params = {"company": str(company_a.id)}
        req_cross.user = emp_b
        assert member_perm.has_permission(req_cross, None) is False

        # IsCEO allows ceo_user, denies dept_head_a
        ceo_perm = IsCEO()
        req_ceo = factory.get("/")
        req_ceo.user = ceo_user
        assert ceo_perm.has_permission(req_ceo, None) is True
        assert ceo_perm.has_permission(req_a, None) is False
