"""
accounts/tests/test_employees_profiles_reporting.py

Tests for Employee CRUD (/api/employees/), Employee Profiles (/api/employees/<id>/profile/),
and Reporting-Manager APIs (/api/employees/<id>/reporting-manager/,
/api/employees/<id>/direct-reports/, /api/employees/<id>/reporting-chain/,
/api/reporting-relationships/, and /api/reporting-relationships/hierarchy/):
- Unauthenticated requests get 401
- CEO full Employee CRUD (create, list/search/filter by department & team, update, soft-deactivate)
- Rich Employee Profile GET & PATCH (self can edit own profile, cannot edit another employee or role -> 403)
- Reporting-manager assignment, reassignment (auto-closes previous active manager preserving history),
  and ending (null / DELETE / /end/)
- Self-reporting rejected (400)
- Circular reporting chain (A -> B -> C -> A) rejected (400)
- Cross-company reporting assignment rejected (400)
- Company In-Charge can manage reporting in own company only (403 on other company)
- Regular Employee gets 403 on assigning/ending reporting managers
- Direct reports, upward reporting chain, and org chart hierarchy endpoints return accurate data
"""
from datetime import date
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import (
    Department,
    DepartmentMembership,
    ReportingRelationship,
    Team,
    TeamMembership,
)
from apps.rbac.roles import SystemRole

EMPLOYEES_URL = "/api/employees/"
REPORTING_URL = "/api/reporting-relationships/"


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
def dept_a(db, company_a):
    return Department.objects.create(
        company=company_a,
        name="Engineering",
        code="ENG",
        status=Department.Status.ACTIVE,
    )


@pytest.fixture
def team_a(db, dept_a):
    return Team.objects.create(
        department=dept_a,
        name="Backend Core",
        code="BE",
        status=Team.Status.ACTIVE,
    )


@pytest.fixture
def ceo_user(db, company_a):
    user = User.objects.create_user(
        email="ceo@alpha.com",
        username="ceo_alpha",
        full_name="CEO Alpha",
        designation="Chief Executive Officer",
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
def incharge_a(db, company_a):
    user = User.objects.create_user(
        email="incharge@alpha.com",
        username="incharge_alpha",
        full_name="Iris InCharge",
        designation="VP Operations",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.COMPANY_IN_CHARGE,
        is_active=True,
    )
    return user


@pytest.fixture
def mgr_a(db, company_a, dept_a, team_a):
    user = User.objects.create_user(
        email="mgr@alpha.com",
        username="mgr_alpha",
        full_name="Morgan Manager",
        designation="Engineering Manager",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.DEPARTMENT_HEAD,
        is_active=True,
    )
    DepartmentMembership.objects.create(
        department=dept_a,
        user=user,
        is_head=True,
        start_date=date.today(),
        status=DepartmentMembership.Status.ACTIVE,
    )
    return user


@pytest.fixture
def emp_a(db, company_a, dept_a, team_a):
    user = User.objects.create_user(
        email="emp@alpha.com",
        username="emp_alpha",
        full_name="Eve Employee",
        employee_id="EMP-010",
        designation="Software Engineer",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_a,
        role=SystemRole.EMPLOYEE,
        is_active=True,
    )
    DepartmentMembership.objects.create(
        department=dept_a,
        user=user,
        is_head=False,
        start_date=date.today(),
        status=DepartmentMembership.Status.ACTIVE,
    )
    TeamMembership.objects.create(
        team=team_a,
        user=user,
        is_leader=False,
        status=TeamMembership.Status.ACTIVE,
    )
    return user


@pytest.fixture
def emp_b(db, company_b):
    user = User.objects.create_user(
        email="bob@beta.com",
        username="bob_beta",
        full_name="Bob Beta",
        designation="Analyst",
        password="UserPassword123!",
        is_active=True,
    )
    CompanyMembership.objects.create(
        user=user,
        company=company_b,
        role=SystemRole.EMPLOYEE,
        is_active=True,
    )
    return user


def auth_client(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class TestEmployeeCRUDAndProfiles:
    def test_unauthenticated_gets_401(self, db, emp_a):
        c = APIClient()
        assert c.get(EMPLOYEES_URL).status_code == 401
        assert c.get(f"{EMPLOYEES_URL}{emp_a.id}/profile/").status_code == 401
        assert c.get(REPORTING_URL).status_code == 401
        assert c.get(f"{REPORTING_URL}hierarchy/").status_code == 401

    def test_ceo_employee_crud_and_org_filters(
        self, db, ceo_user, company_a, dept_a, team_a, emp_a
    ):
        c = auth_client(ceo_user)

        # Create employee at /api/employees/
        r_create = c.post(
            EMPLOYEES_URL,
            {
                "full_name": "Sam Developer",
                "email": "sam@alpha.com",
                "company": str(company_a.id),
                "role": SystemRole.EMPLOYEE,
                "employee_id": "EMP-099",
                "designation": "Backend Engineer",
                "password": "StrongPassword123!",
            },
            format="json",
        )
        assert r_create.status_code == 201
        assert "password" not in r_create.data
        new_id = r_create.data["id"]

        # Filter by department_id & team_id
        r_dept = c.get(f"{EMPLOYEES_URL}?department_id={dept_a.id}")
        assert r_dept.status_code == 200
        dept_results = r_dept.data["results"] if isinstance(r_dept.data, dict) else r_dept.data
        assert any(u["id"] == str(emp_a.id) for u in dept_results)

        r_team = c.get(f"{EMPLOYEES_URL}?team_id={team_a.id}")
        assert r_team.status_code == 200
        team_results = r_team.data["results"] if isinstance(r_team.data, dict) else r_team.data
        assert any(u["id"] == str(emp_a.id) for u in team_results)

        # Update employee
        r_patch = c.patch(
            f"{EMPLOYEES_URL}{new_id}/",
            {"designation": "Senior Backend Engineer"},
            format="json",
        )
        assert r_patch.status_code == 200
        assert r_patch.data["designation"] == "Senior Backend Engineer"

        # Soft delete via DELETE deactivates without removing row
        r_del = c.delete(f"{EMPLOYEES_URL}{new_id}/")
        assert r_del.status_code == 204
        user = User.objects.get(pk=new_id)
        assert user.is_active is False
        assert user.account_status == User.AccountStatus.DEACTIVATED

    def test_rich_employee_profile_and_self_update_permissions(
        self, db, ceo_user, company_a, mgr_a, emp_a
    ):
        # Wire emp_a -> mgr_a -> ceo_user
        ReportingRelationship.objects.create(
            company=company_a, employee=mgr_a, manager=ceo_user, start_date=date.today()
        )
        ReportingRelationship.objects.create(
            company=company_a, employee=emp_a, manager=mgr_a, start_date=date.today()
        )

        # Employee views own rich profile
        c_emp = auth_client(emp_a)
        r_prof = c_emp.get(f"{EMPLOYEES_URL}{emp_a.id}/profile/")
        assert r_prof.status_code == 200
        assert len(r_prof.data["departments"]) == 1
        assert r_prof.data["departments"][0]["department_name"] == "Engineering"
        assert len(r_prof.data["teams"]) == 1
        assert r_prof.data["teams"][0]["team_name"] == "Backend Core"
        assert len(r_prof.data["reporting_managers"]) == 1
        assert r_prof.data["reporting_managers"][0]["id"] == str(mgr_a.id)
        assert [n["id"] for n in r_prof.data["reporting_chain"]] == [
            str(mgr_a.id),
            str(ceo_user.id),
        ]
        assert len(r_prof.data["reporting_history"]) == 1

        # Employee can PATCH own profile fields
        r_self_patch = c_emp.patch(
            f"{EMPLOYEES_URL}{emp_a.id}/profile/",
            {"phone": "+1-555-9999"},
            format="json",
        )
        assert r_self_patch.status_code == 200
        assert r_self_patch.data["phone"] == "+1-555-9999"

        # Employee CANNOT PATCH another employee's profile or change own role -> 403
        assert (
            c_emp.patch(
                f"{EMPLOYEES_URL}{mgr_a.id}/profile/",
                {"phone": "+1-000-0000"},
                format="json",
            ).status_code
            == 403
        )
        assert (
            c_emp.patch(
                f"{EMPLOYEES_URL}{emp_a.id}/profile/",
                {"role": SystemRole.CEO},
                format="json",
            ).status_code
            == 403
        )

        # Manager profile includes emp_a in direct_reports
        c_mgr = auth_client(mgr_a)
        r_mgr_prof = c_mgr.get(f"{EMPLOYEES_URL}{mgr_a.id}/profile/")
        assert r_mgr_prof.status_code == 200
        assert len(r_mgr_prof.data["direct_reports"]) == 1
        assert r_mgr_prof.data["direct_reports"][0]["id"] == str(emp_a.id)


class TestReportingManagerAPIs:
    def test_assign_reassign_end_preserves_history_and_hierarchy(
        self, db, ceo_user, incharge_a, company_a, mgr_a, emp_a
    ):
        c = auth_client(ceo_user)

        # Assign mgr_a as emp_a's reporting manager via /api/employees/<id>/reporting-manager/
        r1 = c.post(
            f"{EMPLOYEES_URL}{emp_a.id}/reporting-manager/",
            {"company": str(company_a.id), "manager": str(mgr_a.id)},
            format="json",
        )
        assert r1.status_code == 201
        rel1_id = r1.data["id"]
        assert r1.data["is_active"] is True

        # Reassign to incharge_a -> previous relationship is closed (end_date set), history preserved
        r2 = c.post(
            f"{EMPLOYEES_URL}{emp_a.id}/reporting-manager/",
            {"company": str(company_a.id), "manager": str(incharge_a.id)},
            format="json",
        )
        assert r2.status_code == 201
        rel1 = ReportingRelationship.objects.get(pk=rel1_id)
        assert rel1.end_date is not None

        # Check direct-reports, reporting-chain, and org hierarchy
        c.post(
            f"{EMPLOYEES_URL}{incharge_a.id}/reporting-manager/",
            {"company": str(company_a.id), "manager": str(ceo_user.id)},
            format="json",
        )
        r_dr = c.get(f"{EMPLOYEES_URL}{incharge_a.id}/direct-reports/")
        assert r_dr.status_code == 200
        assert r_dr.data["count"] == 1
        assert r_dr.data["results"][0]["id"] == str(emp_a.id)

        r_chain = c.get(f"{EMPLOYEES_URL}{emp_a.id}/reporting-chain/")
        assert r_chain.status_code == 200
        assert [n["id"] for n in r_chain.data["chain"]] == [
            str(incharge_a.id),
            str(ceo_user.id),
        ]

        r_hier = c.get(f"{REPORTING_URL}hierarchy/?company={company_a.id}")
        assert r_hier.status_code == 200
        assert len(r_hier.data["nodes"]) >= 4
        assert any(
            e["employee_id"] == str(emp_a.id) and e["manager_id"] == str(incharge_a.id)
            for e in r_hier.data["edges"]
        )

        # End active reporting manager with manager=null
        r_end = c.post(
            f"{EMPLOYEES_URL}{emp_a.id}/reporting-manager/",
            {"company": str(company_a.id), "manager": None},
            format="json",
        )
        assert r_end.status_code == 200
        assert r_end.data["ended_count"] == 1
        assert (
            ReportingRelationship.objects.filter(
                company=company_a, employee=emp_a, end_date__isnull=True
            ).count()
            == 0
        )
        # Historical rows are still preserved
        assert ReportingRelationship.objects.filter(company=company_a, employee=emp_a).count() == 2

    def test_self_report_cycle_and_cross_company_rejected(
        self, db, ceo_user, company_a, mgr_a, emp_a, emp_b
    ):
        c = auth_client(ceo_user)

        # Self-reporting rejected (400)
        r_self = c.post(
            REPORTING_URL,
            {
                "company": str(company_a.id),
                "employee": str(emp_a.id),
                "manager": str(emp_a.id),
            },
            format="json",
        )
        assert r_self.status_code == 400

        # Cross-company manager rejected (400)
        r_cross = c.post(
            REPORTING_URL,
            {
                "company": str(company_a.id),
                "employee": str(emp_a.id),
                "manager": str(emp_b.id),
            },
            format="json",
        )
        assert r_cross.status_code == 400

        # Build chain emp_a -> mgr_a -> ceo_user, then attempt cycle ceo_user -> emp_a -> 400
        assert (
            c.post(
                REPORTING_URL,
                {
                    "company": str(company_a.id),
                    "employee": str(emp_a.id),
                    "manager": str(mgr_a.id),
                },
                format="json",
            ).status_code
            == 201
        )
        assert (
            c.post(
                REPORTING_URL,
                {
                    "company": str(company_a.id),
                    "employee": str(mgr_a.id),
                    "manager": str(ceo_user.id),
                },
                format="json",
            ).status_code
            == 201
        )
        r_cycle = c.post(
            REPORTING_URL,
            {
                "company": str(company_a.id),
                "employee": str(ceo_user.id),
                "manager": str(emp_a.id),
            },
            format="json",
        )
        assert r_cycle.status_code == 400
        assert "manager" in r_cycle.data

    def test_reporting_permissions_company_incharge_and_employee(
        self, db, incharge_a, emp_a, mgr_a, emp_b, company_a, company_b
    ):
        # Employee cannot assign reporting managers -> 403
        c_emp = auth_client(emp_a)
        assert (
            c_emp.post(
                f"{EMPLOYEES_URL}{emp_a.id}/reporting-manager/",
                {"company": str(company_a.id), "manager": str(mgr_a.id)},
                format="json",
            ).status_code
            == 403
        )
        assert (
            c_emp.post(
                REPORTING_URL,
                {
                    "company": str(company_a.id),
                    "employee": str(emp_a.id),
                    "manager": str(mgr_a.id),
                },
                format="json",
            ).status_code
            == 403
        )

        # Company In-Charge can assign in own company, but gets 403 on company_b
        c_ic = auth_client(incharge_a)
        r_ok = c_ic.post(
            f"{EMPLOYEES_URL}{emp_a.id}/reporting-manager/",
            {"company": str(company_a.id), "manager": str(mgr_a.id)},
            format="json",
        )
        assert r_ok.status_code == 201
        rel_id = r_ok.data["id"]

        # End via /api/reporting-relationships/<id>/end/
        r_end = c_ic.post(f"{REPORTING_URL}{rel_id}/end/", format="json")
        assert r_end.status_code == 200
        assert r_end.data["relationship"]["is_active"] is False

        # Cross-company reporting creation rejected with 403
        assert (
            c_ic.post(
                REPORTING_URL,
                {
                    "company": str(company_b.id),
                    "employee": str(emp_b.id),
                    "manager": str(emp_b.id),
                },
                format="json",
            ).status_code
            == 403
        )
