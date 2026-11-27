"""
organization/tests/test_m2_organization_acceptance.py

End-to-End M2 Organization Acceptance Test Suite (Task D1-11):
Validates SRS §27.1 (Organization and Access Acceptance Criteria), §2.8, §4, §5.1–§5.4,
§22, and Database Design §1, §4.1–§4.2, §5 across ALL integrated modules:
- Auth -> Companies -> CompanyMemberships -> RBAC Roles & Permissions ->
  Departments & Heads -> Teams & Leaders -> Employees & Profiles ->
  Reporting Relationships -> Organization Hierarchy & Chart APIs.
"""
from datetime import date
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import Project, ProjectMember
from apps.rbac.models import Role
from apps.rbac.roles import SystemRole
from apps.tasks.models import Task


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def login_client(identifier: str, password: str) -> APIClient:
    cache.clear()
    client = APIClient()
    r = client.post(
        "/api/auth/login/",
        {"identifier": identifier, "password": password},
        format="json",
    )
    assert r.status_code == 200, f"Login failed for {identifier}: {r.data}"
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return client


class TestM2OrganizationAcceptance:
    def test_full_m2_organization_lifecycle_end_to_end(self, db):
        ceo_user = User.objects.create_user(
            email="ceo@tms.local",
            username="ceo_m2",
            full_name="Victoria CEO",
            password="CeoPassword123!",
            is_staff=True,
            is_superuser=True,
            is_active=True,
        )
        c_ceo = login_client("ceo@tms.local", "CeoPassword123!")

        # 1. Create 2 companies
        r_co_a = c_ceo.post(
            "/api/companies/",
            {"name": "Alpha Systems", "code": "ALPHA", "email": "alpha@tms.local"},
            format="json",
        )
        assert r_co_a.status_code == 201
        co_a_id = r_co_a.data["id"]

        r_co_b = c_ceo.post(
            "/api/companies/",
            {"name": "Beta Logistics", "code": "BETA", "email": "beta@tms.local"},
            format="json",
        )
        assert r_co_b.status_code == 201
        co_b_id = r_co_b.data["id"]

        assert (
            c_ceo.post(
                f"/api/companies/{co_a_id}/members/",
                {"user": str(ceo_user.id), "role": SystemRole.CEO},
                format="json",
            ).status_code
            == 201
        )

        # 2. Create employees across Alpha & Beta via /api/employees/
        def create_emp(name, email, company_id, role, emp_id):
            r = c_ceo.post(
                "/api/employees/",
                {
                    "full_name": name,
                    "email": email,
                    "company": company_id,
                    "role": role,
                    "employee_id": emp_id,
                    "password": "UserPassword123!",
                },
                format="json",
            )
            assert r.status_code == 201, r.data
            return r.data["id"]

        ic_id = create_emp("Ian InCharge", "ic@alpha.local", co_a_id, SystemRole.COMPANY_IN_CHARGE, "A-001")
        pm_id = create_emp("Paula PM", "pm@alpha.local", co_a_id, SystemRole.PROJECT_MANAGER, "A-002")
        head_id = create_emp("Hannah Head", "head@alpha.local", co_a_id, SystemRole.DEPARTMENT_HEAD, "A-003")
        lead_id = create_emp("Liam Lead", "lead@alpha.local", co_a_id, SystemRole.TEAM_LEADER, "A-004")
        dev_id = create_emp("Devon Dev", "dev@alpha.local", co_a_id, SystemRole.EMPLOYEE, "A-005")
        create_emp("Bella Beta", "bella@beta.local", co_b_id, SystemRole.EMPLOYEE, "B-001")

        # Real JWT login for every persona
        c_ic = login_client("ic@alpha.local", "UserPassword123!")
        c_head = login_client("head@alpha.local", "UserPassword123!")
        c_dev = login_client("dev@alpha.local", "UserPassword123!")
        c_beta = login_client("bella@beta.local", "UserPassword123!")

        # 3. RBAC enforcement
        assert APIClient().get("/api/rbac/roles/").status_code == 401
        assert (
            c_dev.post("/api/rbac/roles/", {"name": "Hacker", "code": "HACK"}, format="json").status_code
            == 403
        )
        r_custom_role = c_ceo.post(
            "/api/rbac/roles/",
            {"name": "Release Auditor", "code": "ReleaseAuditor", "is_system_role": False},
            format="json",
        )
        assert r_custom_role.status_code == 201
        r_perm = c_ceo.post(
            "/api/rbac/permissions/",
            {"code": "org.hierarchy.view", "description": "View organization hierarchy"},
            format="json",
        )
        assert r_perm.status_code == 201
        assert (
            c_ceo.post(
                "/api/rbac/role-permissions/",
                {"role": r_custom_role.data["id"], "permission": r_perm.data["id"]},
                format="json",
            ).status_code
            == 201
        )
        sys_role = Role.objects.filter(is_system_role=True).first()
        if sys_role is not None:
            assert c_ceo.delete(f"/api/rbac/roles/{sys_role.id}/").status_code == 400

        # 4. Departments, Head assignment, Transfer + active filter + soft-end DELETE
        r_dept_eng = c_ic.post(
            "/api/departments/",
            {"company": co_a_id, "name": "Engineering", "code": "ENG", "department_head": head_id},
            format="json",
        )
        assert r_dept_eng.status_code == 201
        dept_eng_id = r_dept_eng.data["id"]

        r_dept_ops = c_ic.post(
            "/api/departments/",
            {"company": co_a_id, "name": "Operations", "code": "OPS"},
            format="json",
        )
        assert r_dept_ops.status_code == 201
        dept_ops_id = r_dept_ops.data["id"]

        # Duplicate active DepartmentMembership rejected
        assert (
            c_ic.post(
                "/api/organization/department-memberships/",
                {"department": dept_eng_id, "user": head_id, "start_date": date.today().isoformat()},
                format="json",
            ).status_code
            == 400
        )

        # Transfer Devon from OPS -> ENG
        assert c_ic.post(f"/api/departments/{dept_ops_id}/transfer_employee/", {"user": dev_id}, format="json").status_code == 200
        assert c_ic.post(f"/api/departments/{dept_eng_id}/transfer_employee/", {"user": dev_id}, format="json").status_code == 200

        r_ops_emps = c_ceo.get(f"/api/employees/?department_id={dept_ops_id}")
        r_eng_emps = c_ceo.get(f"/api/employees/?department_id={dept_eng_id}")
        ops_ids = {u["id"] for u in (r_ops_emps.data["results"] if isinstance(r_ops_emps.data, dict) else r_ops_emps.data)}
        eng_ids = {u["id"] for u in (r_eng_emps.data["results"] if isinstance(r_eng_emps.data, dict) else r_eng_emps.data)}
        assert dev_id not in ops_ids
        assert dev_id in eng_ids
        assert DepartmentMembership.objects.filter(
            department_id=dept_ops_id, user_id=dev_id, status=DepartmentMembership.Status.TRANSFERRED
        ).exists()

        # Soft-end on DELETE DepartmentMembership
        r_dm_temp = c_ic.post(
            "/api/organization/department-memberships/",
            {"department": dept_ops_id, "user": pm_id, "start_date": date.today().isoformat()},
            format="json",
        )
        assert r_dm_temp.status_code == 201
        dm_temp_id = r_dm_temp.data["id"]
        assert c_ic.delete(f"/api/organization/department-memberships/{dm_temp_id}/").status_code == 204
        dm_temp = DepartmentMembership.objects.get(pk=dm_temp_id)
        assert dm_temp.status == DepartmentMembership.Status.ENDED
        assert dm_temp.end_date is not None

        # 5. Teams, Leader assignment, Add/Remove member + active ?team_id= filter
        r_team = c_head.post(
            "/api/teams/",
            {"department": dept_eng_id, "name": "Platform API", "code": "API", "team_leader": lead_id},
            format="json",
        )
        assert r_team.status_code == 201
        team_id = r_team.data["id"]

        assert (
            c_head.post(
                "/api/organization/team-memberships/",
                {"team": team_id, "user": lead_id},
                format="json",
            ).status_code
            == 400
        )

        assert c_head.post(f"/api/teams/{team_id}/add_member/", {"user": dev_id}, format="json").status_code == 201
        assert c_head.post(f"/api/teams/{team_id}/remove_member/", {"user": dev_id}, format="json").status_code == 200
        r_team_emps = c_ceo.get(f"/api/employees/?team_id={team_id}")
        t_ids = {u["id"] for u in (r_team_emps.data["results"] if isinstance(r_team_emps.data, dict) else r_team_emps.data)}
        assert dev_id not in t_ids
        assert TeamMembership.objects.filter(team_id=team_id, user_id=dev_id, status=TeamMembership.Status.LEFT).exists()

        assert c_head.post(f"/api/teams/{team_id}/add_member/", {"user": dev_id}, format="json").status_code == 201

        # 6. Reporting hierarchy: dev -> lead -> head -> incharge -> ceo
        for emp_x, mgr_x in [(ic_id, str(ceo_user.id)), (head_id, ic_id), (lead_id, head_id), (dev_id, lead_id)]:
            r_rep = c_ic.post(
                f"/api/employees/{emp_x}/reporting-manager/",
                {"company": co_a_id, "manager": mgr_x},
                format="json",
            )
            assert r_rep.status_code == 201, r_rep.data

        # 7. Separate project participation
        proj = Project.objects.create(
            company_id=co_a_id, project_manager_id=pm_id, name="Core ERP", code="ERP", status=Project.Status.ACTIVE
        )
        ProjectMember.objects.create(project=proj, user_id=dev_id, role_in_project="Engineer")
        Task.objects.create(project=proj, title="Ship M2", created_by_id=pm_id, task_owner_id=dev_id, status=Task.Status.IN_PROGRESS)

        # 8. Organization Hierarchy & Employee Detail
        r_hier = c_dev.get("/api/organization/hierarchy/?search=Devon")
        assert r_hier.status_code == 200
        assert r_hier.data["summary"]["matched_employee_ids"] == [dev_id]
        assert r_hier.data["structure"][0]["id"] == co_a_id
        root = r_hier.data["reporting_tree"][0]
        assert root["id"] == str(ceo_user.id)
        assert root["children"][0]["children"][0]["children"][0]["children"][0]["id"] == dev_id

        r_dev_det = c_dev.get(f"/api/organization/hierarchy/employee/{dev_id}/")
        assert r_dev_det.status_code == 200
        assert r_dev_det.data["assigned_projects"][0]["code"] == "ERP"
        assert r_dev_det.data["task_summary"]["active"] == 1
        assert r_dev_det.data["reporting_managers"][0]["id"] == lead_id

        # 9. Cross-company isolation across ALL M2 endpoints
        for url in [
            f"/api/employees/?company={co_a_id}",
            f"/api/departments/?company={co_a_id}",
            f"/api/teams/?company={co_a_id}",
            f"/api/memberships/?company={co_a_id}",
            f"/api/reporting-relationships/?company={co_a_id}",
            f"/api/organization/hierarchy/?company={co_a_id}",
            f"/api/lookup/?company={co_a_id}",
        ]:
            assert c_beta.get(url).status_code == 403, f"Bella should get 403 on {url}"
        assert c_beta.get(f"/api/organization/hierarchy/employee/{dev_id}/").status_code == 404

    def test_inactive_company_and_department_cascade_guards(self, db):
        ceo = User.objects.create_user(
            email="ceo2@tms.local", username="ceo2", full_name="CEO Two",
            password="CeoPassword123!", is_superuser=True, is_active=True,
        )
        c_ceo = APIClient()
        c_ceo.force_authenticate(user=ceo)

        co = Company.objects.create(name="Dormant Corp", code="DORM", is_active=True)
        dept = Department.objects.create(company=co, name="Legacy Dept", code="LEG")
        team = Team.objects.create(department=dept, name="Legacy Team", code="LT")

        dept.deactivate()
        team.deactivate()
        assert c_ceo.post(f"/api/teams/{team.id}/activate/").status_code == 400

        co.soft_delete()
        assert (
            c_ceo.post(
                "/api/departments/",
                {"company": str(co.id), "name": "Blocked Dept"},
                format="json",
            ).status_code
            == 400
        )
        assert c_ceo.post(f"/api/departments/{dept.id}/activate/").status_code == 400
