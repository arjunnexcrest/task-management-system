from datetime import timedelta
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.companies.models import Company
from apps.organization.models import Department, Team
from apps.projects.models import Project
from apps.tasks.models import Task


class RBACAndDashboardsTestCase(TestCase):
    """
    Verifies Role-Based Access Control across all 5 default roles
    (CEO, ProjectManager, DepartmentHead, TeamLeader, Employee)
    and verifies the CEO Executive Dashboard + Role-Specific Dashboard APIs.
    """

    def setUp(self):
        self.client = APIClient()
        call_command("seed_default_users")

        self.ceo = User.objects.get(username="admin")
        self.pm = User.objects.get(username="pm")
        self.depthead = User.objects.get(username="depthead")
        self.teamlead = User.objects.get(username="teamlead")
        self.employee = User.objects.get(username="employee")

        self.company = Company.objects.first()
        self.dept = Department.objects.filter(company=self.company).first()
        self.team = Team.objects.filter(department=self.dept).first()

        self.project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="RBAC Verification Project",
            code="PRJ-RBAC",
            status=Project.Status.ACTIVE,
            priority=Project.Priority.HIGH,
        )
        self.task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="Implement RBAC Guards",
            created_by=self.pm,
            task_owner=self.employee,
            reviewer=self.teamlead,
            priority=Task.Priority.HIGH,
            status=Task.Status.IN_PROGRESS,
            due_date=timezone.now().date() + timedelta(days=2),
            approval_required=True,
        )

    def _auth_as(self, user):
        self.client.force_authenticate(user=user)

    def test_ceo_dashboard_restricted_to_ceo_only(self):
        # Unauthenticated -> 401
        self.client.force_authenticate(user=None)
        self.assertEqual(
            self.client.get("/api/dashboards/ceo/").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

        # Non-CEO roles -> 403 Forbidden
        for non_ceo in (self.pm, self.depthead, self.teamlead, self.employee):
            self._auth_as(non_ceo)
            resp = self.client.get("/api/dashboards/ceo/")
            self.assertEqual(
                resp.status_code,
                status.HTTP_403_FORBIDDEN,
                f"Expected 403 for {non_ceo.username}",
            )

        # CEO -> 200 OK with full enterprise KPIs and company breakdown
        self._auth_as(self.ceo)
        ceo_resp = self.client.get("/api/dashboards/ceo/")
        self.assertEqual(ceo_resp.status_code, status.HTTP_200_OK)
        data = ceo_resp.json()
        self.assertGreaterEqual(data["enterprise_kpis"]["total_companies"], 1)
        self.assertGreaterEqual(len(data["company_breakdown"]), 1)
        self.assertGreaterEqual(len(data["project_health"]), 1)

    def test_role_dashboard_returns_role_specific_scope_and_capabilities(self):
        expectations = [
            (self.ceo, "CEO", True, True, True),
            (self.pm, "ProjectManager", False, False, True),
            (self.depthead, "DepartmentHead", False, False, True),
            (self.teamlead, "TeamLeader", False, False, True),
            (self.employee, "Employee", False, False, False),
        ]
        for usr, expected_role, can_ceo, can_companies, can_review in expectations:
            self._auth_as(usr)
            resp = self.client.get("/api/dashboards/role/")
            self.assertEqual(resp.status_code, status.HTTP_200_OK, usr.username)
            body = resp.json()
            self.assertEqual(body["role"], expected_role)
            self.assertEqual(body["capabilities"]["can_view_ceo_dashboard"], can_ceo)
            self.assertEqual(body["capabilities"]["can_manage_companies"], can_companies)
            self.assertEqual(body["capabilities"]["can_review_tasks"], can_review)
            if expected_role == "Employee":
                self.assertEqual(body["review_queue"], [])
                self.assertEqual(body["team_workload"], [])

    def test_company_crud_strictly_ceo_only(self):
        # Employee, TeamLeader, DeptHead, PM cannot create, edit, or deactivate companies
        for non_ceo in (self.employee, self.teamlead, self.depthead, self.pm):
            self._auth_as(non_ceo)
            self.assertEqual(
                self.client.post(
                    "/api/companies/",
                    {"name": "Unauthorized Corp", "code": "NOPE"},
                    format="json",
                ).status_code,
                status.HTTP_403_FORBIDDEN,
            )
            self.assertEqual(
                self.client.patch(
                    f"/api/companies/{self.company.id}/",
                    {"name": "Hacked Name"},
                    format="json",
                ).status_code,
                status.HTTP_403_FORBIDDEN,
            )
            self.assertEqual(
                self.client.delete(f"/api/companies/{self.company.id}/").status_code,
                status.HTTP_403_FORBIDDEN,
            )

        # CEO CAN create, update (with email/phone/is_active), and soft-deactivate
        self._auth_as(self.ceo)
        create_resp = self.client.post(
            "/api/companies/",
            {
                "name": "Subsidiary Labs",
                "email": "ops@sublabs.local",
                "phone": "+1 555-0199",
                "address": "100 Executive Way",
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        comp_id = create_resp.json()["id"]
        self.assertEqual(create_resp.json()["email"], "ops@sublabs.local")
        self.assertTrue(create_resp.json()["is_active"])

        patch_resp = self.client.patch(
            f"/api/companies/{comp_id}/",
            {"phone": "+1 555-9999"},
            format="json",
        )
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_resp.json()["phone"], "+1 555-9999")

        del_resp = self.client.delete(f"/api/companies/{comp_id}/")
        self.assertEqual(del_resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Company.objects.get(id=comp_id).status, Company.Status.INACTIVE)

    def test_employee_cannot_modify_projects_tasks_departments_teams_users_or_reports(self):
        self._auth_as(self.employee)

        # Cannot create/edit/delete departments or teams
        self.assertEqual(
            self.client.post(
                "/api/organization/departments/",
                {"company": str(self.company.id), "name": "Rogue Dept"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/organization/departments/{self.dept.id}/",
                {"name": "Renamed"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.post(
                "/api/organization/teams/",
                {"department": str(self.dept.id), "name": "Rogue Team"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Cannot create or deactivate users
        self.assertEqual(
            self.client.post(
                "/api/users/",
                {
                    "email": "rogue@tms.local",
                    "full_name": "Rogue User",
                    "password": "User@12345",
                },
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.post(f"/api/users/{self.pm.id}/deactivate/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Cannot create/edit/delete projects
        self.assertEqual(
            self.client.post(
                "/api/projects/",
                {
                    "company": str(self.company.id),
                    "name": "Unauthorized Project",
                    "code": "NOPE-PRJ",
                },
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.delete(f"/api/projects/{self.project.id}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Cannot create/delete tasks or access reports or approve reviews
        self.assertEqual(
            self.client.post(
                "/api/tasks/",
                {"project": str(self.project.id), "title": "Rogue Task"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.delete(f"/api/tasks/{self.task.id}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.get("/api/reports/tasks/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(self.task.id), "decision": "Approved"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # Employee CAN still post comments, submit task for review, and view own dashboard
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{self.task.id}/comments/",
                {"content": "Work completed and ready for review."},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{self.task.id}/submit-for-review/",
                {"notes": "Ready for Team Leader sign-off"},
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )

    def test_pm_can_manage_projects_and_tasks_but_not_delete_projects_or_manage_companies(self):
        self._auth_as(self.pm)
        # PM can edit project and access reports
        self.assertEqual(
            self.client.patch(
                f"/api/projects/{self.project.id}/",
                {"description": "Updated by PM"},
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.get("/api/reports/tasks/").status_code,
            status.HTTP_200_OK,
        )
        # PM cannot hard-delete projects (CEO/CompanyInCharge only)
        self.assertEqual(
            self.client.delete(f"/api/projects/{self.project.id}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        # PM cannot modify RBAC or System Settings
        self.assertEqual(
            self.client.post(
                "/api/rbac/roles/",
                {"name": "Custom", "code": "CUSTOM"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

