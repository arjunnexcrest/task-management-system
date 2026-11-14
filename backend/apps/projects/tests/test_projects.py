import uuid
from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from apps.accounts.models import User
from apps.companies.models import Company
from apps.organization.models import Department, Team
from apps.projects.models import (
    MilestoneDependency,
    MilestoneTask,
    Project,
    ProjectDepartment,
    ProjectMember,
    ProjectMilestone,
    ProjectPhase,
    ProjectTeam,
)
from apps.tasks.models import Task


class ProjectModelsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="pm@test.com",
            username="pmuser",
            full_name="Test PM",
            password="Password123!",
        )
        self.company = Company.objects.create(
            name="Test Company",
            code="TEST-CO",
        )
        self.department = Department.objects.create(
            company=self.company,
            name="Engineering",
            code="ENG",
        )
        self.team = Team.objects.create(
            department=self.department,
            name="Backend Team",
            code="BE",
        )

    def test_project_uuid_pk_and_company_scoped_code(self):
        project = Project.objects.create(
            company=self.company,
            project_manager=self.user,
            name="Alpha Project",
            code="PRJ-ALPHA",
            status=Project.Status.ACTIVE,
            priority=Project.Priority.HIGH,
            budget=Decimal("50000.00"),
        )
        self.assertIsInstance(project.id, uuid.UUID)
        self.assertEqual(project.code, "PRJ-ALPHA")

        # Duplicate code within the same company violates uq_projects_company_code
        with self.assertRaises(IntegrityError):
            Project.objects.create(
                company=self.company,
                name="Duplicate Code Project",
                code="PRJ-ALPHA",
            )

    def test_active_project_requires_manager_validation(self):
        project = Project(
            company=self.company,
            name="No Manager Active Project",
            code="PRJ-NOMGR",
            status=Project.Status.ACTIVE,
        )
        with self.assertRaises(ValidationError):
            project.clean()

    def test_project_phases_milestones_and_dependencies(self):
        project = Project.objects.create(
            company=self.company,
            project_manager=self.user,
            name="Beta Project",
            code="PRJ-BETA",
            status=Project.Status.ACTIVE,
        )
        ProjectMember.objects.create(project=project, user=self.user, role_in_project="PM")
        ProjectDepartment.objects.create(project=project, department=self.department)
        ProjectTeam.objects.create(project=project, team=self.team)

        phase = ProjectPhase.objects.create(
            project=project,
            name="Phase 1",
            owner=self.user,
            status=ProjectPhase.Status.IN_PROGRESS,
        )
        m1 = ProjectMilestone.objects.create(
            project=project,
            title="Milestone 1",
            target_date=date.today() + timedelta(days=10),
            owner=self.user,
        )
        m2 = ProjectMilestone.objects.create(
            project=project,
            title="Milestone 2",
            target_date=date.today() + timedelta(days=20),
            owner=self.user,
        )
        dep = MilestoneDependency.objects.create(milestone=m2, depends_on_milestone=m1)
        self.assertEqual(dep.milestone, m2)

        task = Task.objects.create(
            project=project,
            phase=phase,
            title="Deliverable Task",
            created_by=self.user,
        )
        mt = MilestoneTask.objects.create(milestone=m1, task=task)
        self.assertEqual(mt.task, task)

    def test_project_crud_api_create_read_update_close_archive_delete(self):
        """
        End-to-end API test for Project Create, Read (List + Detail + Filters),
        Update (PUT + PATCH with department/team/member sync), Close, Archive, and Delete.
        """
        from apps.companies.models import CompanyMembership
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.user)

        member_user = User.objects.create_user(
            email="dev@test.com",
            username="devuser",
            full_name="Developer One",
            password="Password123!",
        )
        CompanyMembership.objects.create(company=self.company, user=self.user)
        CompanyMembership.objects.create(company=self.company, user=member_user)
        dept2 = Department.objects.create(
            company=self.company,
            name="Design",
            code="DES",
        )

        # 1. CREATE (POST /api/projects/) with full SRS §6.1 fields & associations
        create_payload = {
            "company": str(self.company.id),
            "project_manager": str(self.user.id),
            "name": "Enterprise ERP Rollout",
            "code": "PRJ-ERP-01",
            "description": "Full ERP implementation across departments.",
            "start_date": str(date.today()),
            "planned_completion_date": str(date.today() + timedelta(days=60)),
            "priority": "Critical",
            "status": "Active",
            "objectives": "Unify finance and engineering workflows.",
            "deliverables": "Production ERP + Training docs.",
            "budget": "125000.00",
            "department_ids": [str(self.department.id), str(dept2.id)],
            "team_ids": [str(self.team.id)],
            "member_ids": [str(member_user.id)],
            "initial_phases": [
                {
                    "name": "Phase 1: Architecture",
                    "start_date": str(date.today()),
                    "end_date": str(date.today() + timedelta(days=15)),
                    "status": "In Progress",
                }
            ],
            "initial_milestones": [
                {
                    "title": "Architecture Sign-off",
                    "target_date": str(date.today() + timedelta(days=15)),
                    "completion_criteria": "Approved blueprint",
                    "approval_required": True,
                }
            ],
        }
        create_res = client.post("/api/projects/", create_payload, format="json")
        self.assertEqual(create_res.status_code, 201, create_res.content)
        project_id = create_res.data["id"]
        self.assertEqual(create_res.data["code"], "PRJ-ERP-01")
        self.assertEqual(len(create_res.data["department_ids"]), 2)
        self.assertEqual(len(create_res.data["team_ids"]), 1)
        self.assertEqual(create_res.data["members_count"], 2)

        # Validation check: duplicate code in same company returns 400
        dup_res = client.post("/api/projects/", create_payload, format="json")
        self.assertEqual(dup_res.status_code, 400)
        self.assertTrue("code" in dup_res.data or "non_field_errors" in dup_res.data)

        # 2. READ LIST & FILTER (GET /api/projects/)
        list_res = client.get(f"/api/projects/?company={self.company.id}&priority=Critical&search=ERP")
        self.assertEqual(list_res.status_code, 200)
        results = list_res.data["results"] if "results" in list_res.data else list_res.data
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], project_id)

        # 3. READ DETAIL (GET /api/projects/{id}/)
        detail_res = client.get(f"/api/projects/{project_id}/")
        self.assertEqual(detail_res.status_code, 200)
        self.assertEqual(len(detail_res.data["project_departments"]), 2)
        self.assertEqual(len(detail_res.data["project_teams"]), 1)
        self.assertEqual(len(detail_res.data["members"]), 2)
        self.assertEqual(len(detail_res.data["phases"]), 1)
        self.assertEqual(len(detail_res.data["milestones"]), 1)
        self.assertIn("dashboard_summary", detail_res.data)

        # 4. UPDATE (PATCH /api/projects/{id}/)
        patch_res = client.patch(
            f"/api/projects/{project_id}/",
            {
                "name": "Enterprise ERP Rollout v2",
                "priority": "High",
                "budget": "150000.00",
                "department_ids": [str(self.department.id)],  # remove dept2
            },
            format="json",
        )
        self.assertEqual(patch_res.status_code, 200, patch_res.content)
        self.assertEqual(patch_res.data["name"], "Enterprise ERP Rollout v2")
        self.assertEqual(patch_res.data["priority"], "High")
        self.assertEqual(len(patch_res.data["department_ids"]), 1)

        # 5. CLOSE PROJECT (POST /api/projects/{id}/close_project/)
        task = Task.objects.create(
            project_id=project_id,
            title="ERP Setup Task",
            created_by=self.user,
            status="Completed",
            progress=100,
        )
        close_res = client.post(
            f"/api/projects/{project_id}/close_project/",
            {"closure_remarks": "All deliverables verified and signed off."},
            format="json",
        )
        self.assertEqual(close_res.status_code, 200, close_res.content)
        self.assertEqual(close_res.data["status"], "Completed")
        self.assertEqual(close_res.data["closure_remarks"], "All deliverables verified and signed off.")

        # 6. ARCHIVE PROJECT (POST /api/projects/{id}/archive/)
        archive_res = client.post(f"/api/projects/{project_id}/archive/")
        self.assertEqual(archive_res.status_code, 200)
        self.assertEqual(archive_res.data["status"], "Archived")

        # 7. DELETE PROJECT (DELETE /api/projects/{id}/) even when tasks exist
        del_res = client.delete(f"/api/projects/{project_id}/")
        self.assertEqual(del_res.status_code, 204)
        self.assertFalse(Project.objects.filter(id=project_id).exists())
        self.assertFalse(Task.objects.filter(id=task.id).exists())

    def test_project_manager_assignment_and_department_mapping_workflow(self):
        """
        Tests SRS §6.3 Project Assignment Workflow & DB Design §4.3 / §5:
        1. Assign / Reassign Project Manager (POST /api/projects/{id}/assign_manager/)
        2. Map Participating Departments & auto-coordinate Department Heads (POST /api/projects/{id}/map_departments/)
        3. Map Participating Teams & auto-coordinate Team Leaders (POST /api/projects/{id}/map_teams/)
        4. Assign & soft-remove Project Members while preserving membership history (POST /api/projects/{id}/assign_members/)
        5. Retrieve full Assignment Hierarchy (GET /api/projects/{id}/assignment_hierarchy/)
        """
        from apps.companies.models import CompanyMembership
        from apps.notifications.models import Notification
        from apps.organization.models import DepartmentMembership, TeamMembership
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.user)

        pm2 = User.objects.create_user(
            email="pm2@test.com",
            username="pm2user",
            full_name="Second PM",
            password="Password123!",
        )
        dept_head = User.objects.create_user(
            email="head@test.com",
            username="headuser",
            full_name="Engineering Head",
            password="Password123!",
        )
        team_leader = User.objects.create_user(
            email="lead@test.com",
            username="leaduser",
            full_name="Backend Lead",
            password="Password123!",
        )
        for u in (self.user, pm2, dept_head, team_leader):
            CompanyMembership.objects.get_or_create(company=self.company, user=u)

        DepartmentMembership.objects.create(
            department=self.department,
            user=dept_head,
            is_head=True,
            start_date=date.today(),
            status="Active",
        )
        TeamMembership.objects.create(
            team=self.team,
            user=team_leader,
            is_leader=True,
            status="Active",
        )

        project = Project.objects.create(
            company=self.company,
            name="Assignment Foundation Project",
            code="PRJ-ASSIGN-01",
            status=Project.Status.DRAFT,
        )

        # 1. Assign first PM with auto_activate=True
        res_pm1 = client.post(
            f"/api/projects/{project.id}/assign_manager/",
            {"project_manager": str(self.user.id), "auto_activate": True},
            format="json",
        )
        self.assertEqual(res_pm1.status_code, 200, res_pm1.content)
        self.assertEqual(str(res_pm1.data["project_manager"]), str(self.user.id))
        self.assertEqual(res_pm1.data["status"], "Active")

        # Reassign to Second PM -> first PM's membership history is preserved as 'Former Project Manager'
        res_pm2 = client.post(
            f"/api/projects/{project.id}/assign_manager/",
            {"project_manager": str(pm2.id)},
            format="json",
        )
        self.assertEqual(res_pm2.status_code, 200, res_pm2.content)
        self.assertEqual(str(res_pm2.data["project_manager"]), str(pm2.id))
        self.assertTrue(
            ProjectMember.objects.filter(
                project=project, user=self.user, role_in_project="Former Project Manager"
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                user=pm2, related_project=project, notification_type="PROJECT_MANAGER_ASSIGNED"
            ).exists()
        )

        # 2. Map Participating Department -> auto-adds Department Head & sends notification
        res_dept = client.post(
            f"/api/projects/{project.id}/map_departments/",
            {"department_ids": [str(self.department.id)], "replace": True, "auto_add_heads": True},
            format="json",
        )
        self.assertEqual(res_dept.status_code, 200, res_dept.content)
        self.assertEqual(len(res_dept.data["project_departments"]), 1)
        self.assertTrue(
            ProjectMember.objects.filter(project=project, user=dept_head, status="Active").exists()
        )

        # Cross-company department mapping must be rejected
        other_company = Company.objects.create(name="Other Co", code="OTHER-CO")
        other_dept = Department.objects.create(company=other_company, name="External Dept", code="EXT")
        res_bad_dept = client.post(
            f"/api/projects/{project.id}/map_departments/",
            {"department_ids": [str(other_dept.id)]},
            format="json",
        )
        self.assertEqual(res_bad_dept.status_code, 400)

        # 3. Map Participating Team -> auto-adds Team Leader
        res_team = client.post(
            f"/api/projects/{project.id}/map_teams/",
            {"team_ids": [str(self.team.id)], "replace": True, "auto_add_leaders": True},
            format="json",
        )
        self.assertEqual(res_team.status_code, 200, res_team.content)
        self.assertEqual(len(res_team.data["project_teams"]), 1)
        self.assertTrue(
            ProjectMember.objects.filter(project=project, user=team_leader, status="Active").exists()
        )

        # 4. Soft-remove a Project Member and verify history is retained (status='Removed', left_at!=None)
        tl_member = ProjectMember.objects.get(project=project, user=team_leader)
        del_mem_res = client.delete(f"/api/projects/members/{tl_member.id}/")
        self.assertEqual(del_mem_res.status_code, 200)
        tl_member.refresh_from_db()
        self.assertEqual(tl_member.status, ProjectMember.Status.REMOVED)
        self.assertIsNotNone(tl_member.left_at)

        # Re-assigning reactivates the historical ProjectMember record
        res_reactivate = client.post(
            f"/api/projects/{project.id}/assign_members/",
            {"members": [{"user": str(team_leader.id), "role_in_project": "Lead Backend Engineer"}]},
            format="json",
        )
        self.assertEqual(res_reactivate.status_code, 200)
        tl_member.refresh_from_db()
        self.assertEqual(tl_member.status, ProjectMember.Status.ACTIVE)
        self.assertIsNone(tl_member.left_at)

        # 5. Assignment Hierarchy endpoint (SRS §6.3)
        hier_res = client.get(f"/api/projects/{project.id}/assignment_hierarchy/")
        self.assertEqual(hier_res.status_code, 200)
        self.assertEqual(hier_res.data["project_manager"]["id"], str(pm2.id))
        self.assertEqual(len(hier_res.data["participating_departments"]), 1)
        self.assertEqual(
            hier_res.data["participating_departments"][0]["department_head"]["id"],
            str(dept_head.id),
        )
        self.assertEqual(
            len(hier_res.data["participating_departments"][0]["participating_teams"]), 1
        )
