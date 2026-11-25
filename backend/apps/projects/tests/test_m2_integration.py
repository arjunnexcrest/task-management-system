from datetime import date, timedelta
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import Notification
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import Project, ProjectMember
from apps.system_settings.models import SystemSetting
from apps.tasks.models import ChecklistItem, Task, TaskComment


class M2ProjectTaskEndToEndIntegrationTestCase(APITestCase):
    """
    Task #10 (Subtasks, Checklists, Comments) & Task #11 (Complete Project/Task End-to-End Integration — M2 Acceptance).
    Validates SRS §6.1–§6.6, §7.1–§7.11, §8.1–§8.4, §13.1, §14.3, §26 Phase 2 & §27.2–§27.3.
    """

    def setUp(self):
        self.ceo = User.objects.create_user(
            email="ceo_m2@acme.com",
            username="ceo_m2",
            full_name="CEO M2",
            password="Password123!",
            is_superuser=True,
        )
        self.pm = User.objects.create_user(
            email="pm_m2@acme.com",
            username="pm_m2",
            full_name="Project Manager M2",
            password="Password123!",
        )
        self.dept_head = User.objects.create_user(
            email="head_m2@acme.com",
            username="head_m2",
            full_name="Dept Head M2",
            password="Password123!",
        )
        self.dev = User.objects.create_user(
            email="dev_m2@acme.com",
            username="dev_m2",
            full_name="Developer M2",
            password="Password123!",
        )
        self.company = Company.objects.create(
            name="M2 Enterprise Corp",
            code="M2-CORP",
        )
        from apps.rbac.models import MembershipRole, Role

        pm_role, _ = Role.objects.get_or_create(
            code="ProjectManager", defaults={"name": "Project Manager", "is_system_role": True}
        )
        ceo_role, _ = Role.objects.get_or_create(
            code="CEO", defaults={"name": "CEO", "is_system_role": True}
        )
        self.ceo.is_superuser = True
        self.ceo.save(update_fields=["is_superuser"])
        for u in (self.ceo, self.pm, self.dept_head, self.dev):
            cm = CompanyMembership.objects.create(company=self.company, user=u, status="Active")
            if u == self.ceo:
                MembershipRole.objects.create(membership=cm, role=ceo_role)
            elif u == self.pm:
                MembershipRole.objects.create(membership=cm, role=pm_role)

        self.dept = Department.objects.create(
            company=self.company,
            name="Engineering",
            code="ENG-M2",
        )
        DepartmentMembership.objects.create(
            department=self.dept,
            user=self.dept_head,
            is_head=True,
            start_date=date.today(),
            status="Active",
        )
        self.team = Team.objects.create(
            department=self.dept,
            name="Core API Team",
            code="CORE-M2",
        )
        TeamMembership.objects.create(
            team=self.team, user=self.dev, is_leader=True, status="Active"
        )
        self.client.force_authenticate(user=self.pm)

    def test_m2_end_to_end_project_and_task_lifecycle(self):
        # 1. Create Project (Draft) -> Assign PM (auto_activate) -> Map Dept & Team
        proj_res = self.client.post(
            "/api/projects/",
            {
                "company": str(self.company.id),
                "name": "M2 Integrated Platform",
                "code": "PRJ-M2-01",
                "priority": "High",
                "status": "Draft",
                "start_date": str(date.today()),
                "planned_completion_date": str(date.today() + timedelta(days=30)),
            },
            format="json",
        )
        self.assertEqual(proj_res.status_code, status.HTTP_201_CREATED, proj_res.content)
        project_id = proj_res.data["id"]

        self.client.force_authenticate(user=self.ceo)
        pm_res = self.client.post(
            f"/api/projects/{project_id}/assign_manager/",
            {"project_manager": str(self.pm.id), "auto_activate": True},
            format="json",
        )
        self.assertEqual(pm_res.status_code, 200)
        self.assertEqual(pm_res.data["status"], "Active")
        self.client.force_authenticate(user=self.pm)

        dept_res = self.client.post(
            f"/api/projects/{project_id}/map_departments/",
            {"department_ids": [str(self.dept.id)], "auto_add_heads": True},
            format="json",
        )
        self.assertEqual(dept_res.status_code, 200)

        team_res = self.client.post(
            f"/api/projects/{project_id}/map_teams/",
            {
                "team_ids": [str(self.team.id)],
                "auto_add_leaders": True,
                "auto_add_team_members": True,
            },
            format="json",
        )
        self.assertEqual(team_res.status_code, 200)
        self.assertTrue(
            ProjectMember.objects.filter(project_id=project_id, user=self.dev, status="Active").exists()
        )

        # 2. Create Phase & Milestone
        phase_res = self.client.post(
            "/api/projects/phases/",
            {"project": project_id, "name": "Phase 1: Delivery", "status": "In Progress"},
            format="json",
        )
        self.assertEqual(phase_res.status_code, 201)
        ms_res = self.client.post(
            "/api/projects/milestones/",
            {
                "project": project_id,
                "title": "M2 Acceptance Milestone",
                "target_date": str(date.today() + timedelta(days=15)),
            },
            format="json",
        )
        self.assertEqual(ms_res.status_code, 201)

        # 3. Create Top-Level Task with progress_method='Subtasks' & link to Milestone
        task_res = self.client.post(
            "/api/tasks/",
            {
                "project": project_id,
                "phase": phase_res.data["id"],
                "department": str(self.dept.id),
                "team": str(self.team.id),
                "title": "Implement Payment Gateway",
                "task_owner": str(self.dev.id),
                "reviewer": str(self.pm.id),
                "priority": "Critical",
                "status": "To Do",
                "progress_method": "Subtasks",
                "approval_required": True,
                "assignee_user_ids": [str(self.dev.id)],
            },
            format="json",
        )
        self.assertEqual(task_res.status_code, 201, task_res.content)
        parent_task_id = task_res.data["id"]

        mt_res = self.client.post(
            "/api/projects/milestone-tasks/",
            {"milestone": ms_res.data["id"], "task": parent_task_id},
            format="json",
        )
        self.assertEqual(mt_res.status_code, 201)

        # 4. Subtask Depth Limit enforcement (SRS §8.1 & §20.2)
        SystemSetting.objects.create(
            company=self.company,
            setting_key="subtask_max_depth",
            setting_value=1,
        )
        sub_res = self.client.post(
            "/api/tasks/",
            {
                "project": project_id,
                "parent_task": parent_task_id,
                "title": "Configure Stripe Webhook",
                "task_owner": str(self.dev.id),
                "progress_method": "Checklist",
            },
            format="json",
        )
        self.assertEqual(sub_res.status_code, 201, sub_res.content)
        subtask_id = sub_res.data["id"]

        # Depth 2 subtask must be rejected when subtask_max_depth=1
        deep_sub_res = self.client.post(
            "/api/tasks/",
            {
                "project": project_id,
                "parent_task": subtask_id,
                "title": "Nested Depth 2 Subtask",
            },
            format="json",
        )
        self.assertEqual(deep_sub_res.status_code, 400)

        # 5. Checklists: Create, Add Items with notes & link, Reorder, Toggle -> Auto-progress rollup
        cl_res = self.client.post(
            "/api/tasks/checklists/",
            {"task": subtask_id, "title": "Webhook Verification Steps"},
            format="json",
        )
        self.assertEqual(cl_res.status_code, 201)
        checklist_id = cl_res.data["id"]

        item1_res = self.client.post(
            "/api/tasks/checklist-items/",
            {
                "checklist": checklist_id,
                "title": "Verify HMAC signature",
                "notes": "Use SHA-256 secret",
                "link": "https://stripe.com/docs/webhooks",
                "sort_order": 1,
            },
            format="json",
        )
        item2_res = self.client.post(
            "/api/tasks/checklist-items/",
            {
                "checklist": checklist_id,
                "title": "Handle idempotency keys",
                "sort_order": 2,
            },
            format="json",
        )
        self.assertEqual(item1_res.status_code, 201)
        self.assertEqual(item2_res.status_code, 201)

        # Reorder items (item2 first, item1 second)
        reorder_res = self.client.post(
            f"/api/tasks/checklists/{checklist_id}/reorder_items/",
            {"item_ids": [item2_res.data["id"], item1_res.data["id"]]},
            format="json",
        )
        self.assertEqual(reorder_res.status_code, 200)
        self.assertEqual(
            ChecklistItem.objects.get(id=item2_res.data["id"]).sort_order, 1
        )

        # Toggle both checklist items -> subtask progress becomes 100%, parent task progress becomes 100%
        self.client.post(f"/api/tasks/checklist-items/{item1_res.data['id']}/toggle/")
        self.client.post(f"/api/tasks/checklist-items/{item2_res.data['id']}/toggle/")
        sub_obj = Task.objects.get(id=subtask_id)
        parent_obj = Task.objects.get(id=parent_task_id)
        self.assertEqual(float(sub_obj.progress), 100.0)
        self.assertEqual(float(parent_obj.progress), 100.0)

        # 6. Threaded Comments, Replies & @Mentions (SRS §7.7 & §13.1)
        c1_res = self.client.post(
            f"/api/tasks/{parent_task_id}/comments/",
            {
                "content": "Webhook checklist verified, ready for review.",
                "mentioned_user_ids": [str(self.dept_head.id)],
            },
            format="json",
        )
        self.assertEqual(c1_res.status_code, 201, c1_res.content)
        self.assertEqual(len(c1_res.data["mentions"]), 1)
        self.assertTrue(
            Notification.objects.filter(
                user=self.dept_head, notification_type="COMMENT_MENTION"
            ).exists()
        )

        reply_res = self.client.post(
            "/api/tasks/comments/",
            {
                "task": parent_task_id,
                "parent_comment": c1_res.data["id"],
                "content": "Acknowledged, proceeding to final approval.",
            },
            format="json",
        )
        self.assertEqual(reply_res.status_code, 201)
        self.assertEqual(TaskComment.objects.filter(task_id=parent_task_id, parent_comment__isnull=False).count(), 1)

        # 7. Complete Subtask & Parent Task Workflow -> Close Project (SRS §6.6 & §7.3)
        self.client.post(f"/api/tasks/{subtask_id}/transition-status/", {"status": "In Progress"}, format="json")
        self.client.post(f"/api/tasks/{subtask_id}/transition-status/", {"status": "Completed"}, format="json")

        for st in ("In Progress", "In Review", "Approved", "Completed"):
            tr = self.client.post(
                f"/api/tasks/{parent_task_id}/transition-status/",
                {"status": st},
                format="json",
            )
            self.assertEqual(tr.status_code, 200, tr.content)

        close_res = self.client.post(
            f"/api/projects/{project_id}/close_project/",
            {"closure_remarks": "M2 end-to-end project and task acceptance verified."},
            format="json",
        )
        self.assertEqual(close_res.status_code, 200, close_res.content)
        self.assertEqual(close_res.data["status"], "Completed")
        self.assertEqual(float(Project.objects.get(id=project_id).progress), 100.0)
