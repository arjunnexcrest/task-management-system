"""
companies/tests/test_company_access_restrictions.py

Comprehensive tests for D1-09:
"Implement employee-company relationships and access restrictions"
Deliverable: "Company membership restrictions tested"

Covers:
1. Unassigned employee (zero active memberships) blocked with 403 across all company-scoped
   endpoints (/api/employees/, /api/departments/, /api/teams/, /api/memberships/,
   /api/reporting-relationships/, /api/lookup/, /api/companies/my-companies/), while
   /api/users/me/ remains accessible.
2. Cross-company isolation via query params (?company=), X-Company-ID header, and direct
   object IDs across all endpoints (including /api/lookup/).
3. Multi-company employee with distinct per-company roles (e.g., DepartmentHead in Company A,
   Employee in Company B, non-member of Company C): role permissions are strictly scoped
   per company, and /api/companies/my-companies/ lists only their active companies.
4. Immediate access revocation when a CompanyMembership is deactivated, and immediate
   restoration when reactivated.
5. Soft-deleted/inactive company revokes non-CEO member access (403) while CEO retains access;
   adding/activating memberships in an inactive company or for an inactive user is rejected (400);
   changing user/company on an existing membership is rejected (400).
6. Employee-company relationship endpoints (/api/companies/<id>/members/ and
   /api/employees/<id>/companies/).
"""
from datetime import date
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import Department, DepartmentMembership, ReportingRelationship, Team
from apps.rbac.roles import SystemRole


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def company_a(db):
    return Company.objects.create(name="Alpha Corp", code="ALPHA", email="a@alpha.com", is_active=True)


@pytest.fixture
def company_b(db):
    return Company.objects.create(name="Beta Ltd", code="BETA", email="b@beta.com", is_active=True)


@pytest.fixture
def company_c(db):
    return Company.objects.create(name="Gamma Inc", code="GAMMA", email="c@gamma.com", is_active=True)


@pytest.fixture
def ceo(db, company_a):
    user = User.objects.create_user(
        email="ceo@alpha.com",
        username="ceo_alpha",
        full_name="CEO Alpha",
        password="CeoPassword123!",
        is_staff=True,
        is_active=True,
    )
    CompanyMembership.objects.create(user=user, company=company_a, role=SystemRole.CEO, is_active=True)
    return user


@pytest.fixture
def emp_a(db, company_a):
    user = User.objects.create_user(
        email="alice@alpha.com",
        username="alice_alpha",
        full_name="Alice Alpha",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(user=user, company=company_a, role=SystemRole.EMPLOYEE, is_active=True)
    return user


@pytest.fixture
def emp_b(db, company_b):
    user = User.objects.create_user(
        email="bob@beta.com",
        username="bob_beta",
        full_name="Bob Beta",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(user=user, company=company_b, role=SystemRole.EMPLOYEE, is_active=True)
    return user


@pytest.fixture
def multi_co_user(db, company_a, company_b):
    user = User.objects.create_user(
        email="maya@multi.com",
        username="maya_multi",
        full_name="Maya Multi",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user, company=company_a, role=SystemRole.DEPARTMENT_HEAD, is_active=True
    )
    CompanyMembership.objects.create(
        user=user, company=company_b, role=SystemRole.EMPLOYEE, is_active=True
    )
    return user


@pytest.fixture
def unassigned_user(db):
    return User.objects.create_user(
        email="unassigned@tms.local",
        username="unassigned",
        full_name="Unassigned User",
        password="UserPassword123!",
        is_active=True,
    )


@pytest.fixture
def org_data(db, company_a, company_b, company_c, emp_a, emp_b, multi_co_user):
    dept_a = Department.objects.create(company=company_a, name="Alpha Eng", code="A-ENG")
    dept_b = Department.objects.create(company=company_b, name="Beta Sales", code="B-SLS")
    dept_c = Department.objects.create(company=company_c, name="Gamma Ops", code="G-OPS")

    DepartmentMembership.objects.create(
        department=dept_a, user=multi_co_user, is_head=True, start_date=date.today()
    )

    team_a = Team.objects.create(department=dept_a, name="Alpha Core", code="A-CORE")
    team_b = Team.objects.create(department=dept_b, name="Beta Direct", code="B-DIR")

    rel_b = ReportingRelationship.objects.create(
        company=company_b, employee=emp_b, manager=multi_co_user, start_date=date.today()
    )
    return {
        "dept_a": dept_a,
        "dept_b": dept_b,
        "dept_c": dept_c,
        "team_a": team_a,
        "team_b": team_b,
        "rel_b": rel_b,
    }


def auth_client(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TestCompanyMembershipAccessRestrictions:
    def test_unassigned_user_blocked_everywhere_except_me(self, db, unassigned_user, org_data):
        c = auth_client(unassigned_user)

        for url in [
            "/api/employees/",
            "/api/users/",
            "/api/memberships/",
            "/api/departments/",
            "/api/teams/",
            "/api/reporting-relationships/",
            "/api/lookup/",
            "/api/companies/my-companies/",
        ]:
            assert c.get(url).status_code == 403, f"Expected 403 on {url} for unassigned user"

        # Own profile stays accessible
        r_me = c.get("/api/users/me/")
        assert r_me.status_code == 200
        assert r_me.data["memberships"] == []

    def test_cross_company_isolation_via_query_header_and_direct_ids(
        self, db, emp_a, emp_b, company_a, company_b, org_data
    ):
        c_a = auth_client(emp_a)
        # Unauthenticated lookup gets 401
        assert APIClient().get("/api/lookup/").status_code == 401

        # Querying Company B via ?company= returns 403 across all endpoints
        for url in [
            f"/api/employees/?company={company_b.id}",
            f"/api/memberships/?company={company_b.id}",
            f"/api/departments/?company={company_b.id}",
            f"/api/teams/?company={company_b.id}",
            f"/api/reporting-relationships/?company={company_b.id}",
            f"/api/lookup/?company={company_b.id}",
        ]:
            assert c_a.get(url).status_code == 403, f"Expected 403 on {url}"

        # Querying Company B via X-Company-ID header returns 403 across all endpoints
        headers = {"HTTP_X_COMPANY_ID": str(company_b.id)}
        for url in [
            "/api/employees/",
            "/api/memberships/",
            "/api/departments/",
            "/api/teams/",
            "/api/reporting-relationships/",
            "/api/lookup/",
        ]:
            assert c_a.get(url, **headers).status_code == 403, f"Expected 403 with X-Company-ID on {url}"

        # Direct object URLs for Company B resources never leak data
        assert c_a.get(f"/api/employees/{emp_b.id}/").status_code in (403, 404)
        assert c_a.get(f"/api/employees/{emp_b.id}/profile/").status_code in (403, 404)
        assert c_a.get(f"/api/departments/{org_data['dept_b'].id}/").status_code in (403, 404)
        assert c_a.get(f"/api/teams/{org_data['team_b'].id}/").status_code in (403, 404)
        assert c_a.get(f"/api/reporting-relationships/{org_data['rel_b'].id}/").status_code in (403, 404)

        # /api/lookup/ returns ONLY Company A data for emp_a
        r_lookup = c_a.get("/api/lookup/")
        assert r_lookup.status_code == 200
        assert {str(c["id"]) for c in r_lookup.data["companies"]} == {str(company_a.id)}
        assert {d["id"] for d in r_lookup.data["departments"]} == {str(org_data["dept_a"].id)}
        assert {t["id"] for t in r_lookup.data["teams"]} == {str(org_data["team_a"].id)}
        lookup_user_ids = {u["id"] for u in r_lookup.data["users"]}
        assert str(emp_a.id) in lookup_user_ids
        assert str(emp_b.id) not in lookup_user_ids

    def test_multi_company_employee_scoped_roles_and_my_companies(
        self, db, multi_co_user, company_a, company_b, company_c, org_data
    ):
        c = auth_client(multi_co_user)

        # /api/companies/my-companies/ returns Company A & B only (excludes C)
        r_mine = c.get("/api/companies/my-companies/")
        assert r_mine.status_code == 200
        my_ids = {comp["id"] for comp in r_mine.data}
        assert my_ids == {str(company_a.id), str(company_b.id)}
        assert str(company_c.id) not in my_ids

        # Can read both Company A & B, blocked on Company C
        assert c.get(f"/api/departments/?company={company_a.id}").status_code == 200
        assert c.get(f"/api/departments/?company={company_b.id}").status_code == 200
        assert c.get(f"/api/departments/?company={company_c.id}").status_code == 403

        # Can create a team in Company A (where DepartmentHead), but gets 403 in Company B (where Employee)
        r_ok = c.post(
            "/api/teams/",
            {"department": str(org_data["dept_a"].id), "name": "Alpha Mobile"},
            format="json",
        )
        assert r_ok.status_code == 201

        r_denied = c.post(
            "/api/teams/",
            {"department": str(org_data["dept_b"].id), "name": "Beta Rogue"},
            format="json",
        )
        assert r_denied.status_code == 403

    def test_deactivating_and_reactivating_membership_toggles_access_immediately(
        self, db, ceo, emp_a, company_a, org_data
    ):
        c_ceo = auth_client(ceo)
        c_emp = auth_client(emp_a)
        membership = CompanyMembership.objects.get(user=emp_a, company=company_a)

        # Active member has access
        assert c_emp.get(f"/api/departments/?company={company_a.id}").status_code == 200
        assert c_emp.get(f"/api/lookup/?company={company_a.id}").status_code == 200

        # CEO deactivates membership -> immediate 403 everywhere for emp_a
        assert c_ceo.post(f"/api/memberships/{membership.id}/deactivate/").status_code == 200
        assert c_emp.get(f"/api/departments/?company={company_a.id}").status_code == 403
        assert c_emp.get(f"/api/teams/?company={company_a.id}").status_code == 403
        assert c_emp.get(f"/api/employees/?company={company_a.id}").status_code == 403
        assert c_emp.get(f"/api/lookup/?company={company_a.id}").status_code == 403

        # CEO reactivates membership -> immediate 200 access restored
        assert c_ceo.post(f"/api/memberships/{membership.id}/activate/").status_code == 200
        assert c_emp.get(f"/api/departments/?company={company_a.id}").status_code == 200
        assert c_emp.get(f"/api/lookup/?company={company_a.id}").status_code == 200

    def test_inactive_company_revokes_member_access_and_blocks_new_memberships(
        self, db, ceo, emp_a, unassigned_user, company_a, company_b, org_data
    ):
        c_ceo = auth_client(ceo)
        c_emp = auth_client(emp_a)

        # Soft-delete company_a -> emp_a loses access immediately (403)
        assert c_ceo.delete(f"/api/companies/{company_a.id}/").status_code == 204
        assert c_emp.get(f"/api/departments/?company={company_a.id}").status_code == 403
        assert c_emp.get("/api/lookup/").status_code == 403

        # CEO still sees the deactivated company
        assert c_ceo.get(f"/api/companies/{company_a.id}/").status_code == 200

        # Adding a new member to an inactive company is rejected (400)
        r_inactive_co = c_ceo.post(
            f"/api/companies/{company_a.id}/members/",
            {"user": str(unassigned_user.id), "role": SystemRole.EMPLOYEE},
            format="json",
        )
        assert r_inactive_co.status_code == 400

        # Adding an inactive user to an active company is rejected (400)
        unassigned_user.deactivate()
        r_inactive_user = c_ceo.post(
            f"/api/companies/{company_b.id}/members/",
            {"user": str(unassigned_user.id), "role": SystemRole.EMPLOYEE},
            format="json",
        )
        assert r_inactive_user.status_code == 400

    def test_employee_company_relationship_endpoints_and_immutable_keys(
        self, db, ceo, emp_a, emp_b, unassigned_user, company_a, company_b
    ):
        c_ceo = auth_client(ceo)
        c_emp = auth_client(emp_a)

        # CEO adds unassigned_user to company_a via /api/companies/<id>/members/
        r_add = c_ceo.post(
            f"/api/companies/{company_a.id}/members/",
            {"user": str(unassigned_user.id), "role": SystemRole.PROJECT_MANAGER},
            format="json",
        )
        assert r_add.status_code == 201
        m_id = r_add.data["id"]
        assert r_add.data["role"] == SystemRole.PROJECT_MANAGER

        # CEO adds unassigned_user to company_b via /api/employees/<id>/companies/
        r_add_b = c_ceo.post(
            f"/api/employees/{unassigned_user.id}/companies/",
            {"company": str(company_b.id), "role": SystemRole.EMPLOYEE},
            format="json",
        )
        assert r_add_b.status_code == 201

        # CEO sees both memberships of unassigned_user
        r_ceo_list = c_ceo.get(f"/api/employees/{unassigned_user.id}/companies/")
        assert r_ceo_list.status_code == 200
        assert len(r_ceo_list.data) == 2

        # emp_a (only in Company A) sees ONLY the shared Company A membership of unassigned_user
        r_emp_list = c_emp.get(f"/api/employees/{unassigned_user.id}/companies/")
        assert r_emp_list.status_code == 200
        assert len(r_emp_list.data) == 1
        assert str(r_emp_list.data[0]["company"]) == str(company_a.id)

        # Non-CEO cannot add employee to company (403)
        assert (
            c_emp.post(
                f"/api/employees/{unassigned_user.id}/companies/",
                {"company": str(company_b.id), "role": SystemRole.EMPLOYEE},
                format="json",
            ).status_code
            == 403
        )

        # Changing user or company on an existing membership via PATCH is rejected (400)
        assert (
            c_ceo.patch(
                f"/api/memberships/{m_id}/",
                {"company": str(company_b.id)},
                format="json",
            ).status_code
            == 400
        )
        assert (
            c_ceo.patch(
                f"/api/memberships/{m_id}/",
                {"user": str(emp_b.id)},
                format="json",
            ).status_code
            == 400
        )
