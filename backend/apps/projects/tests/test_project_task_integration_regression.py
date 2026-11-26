from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import Notification
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import (
    MilestoneDependency,
    MilestoneTask,
    Project,
    ProjectMilestone,
    ProjectPhase,
)
from apps.rbac.models import MembershipRole, Role
from apps.risks.models import Issue, Risk
from apps.tasks.models import Task, TaskDependency


class ProjectTaskIntegrationRegressionTestCase(APITestCase):
    """
    Task #17: Complete project/task integration and regression (Module code-complete).
    Validates end-to-end cross-module behavior across Projects, Phases, Milestones,
    MilestoneDependencies, Tasks, Subtasks, TaskDependencies, Time Tracking (1-active-timer),
    Notifications, Risks/Issues/Blockers, Filtered CSV Reports, Multi-Company Isolation,
    and RBAC Dashboards.
    """

    def setUp(self):
        self.today = timezone.now().date()

        self.role_ceo, _ = Role.objects.get_or_create(
            code="CEO", defaults={"name": "CEO", "is_system_role": True}
        )
        self.role_pm, _ = Role.objects.get_or_create(
            code="ProjectManager", defaults={"name": "Project Manager", "is_system_role": True}
        )
        self.role_head, _ = Role.objects.get_or_create(
            code="DepartmentHead", defaults={"name": "Department Head", "is_system_role": True}
        )
        self.role_lead, _ = Role.objects.get_or_create(
            code="TeamLeader", defaults={"name": "Team Leader", "is_system_role": True}
        )
        self.role_emp, _ = Role.objects.get_or_create(
            code="Employee", defaults={"name": "Employee", "is_system_role": True}
        )

        self.company_a = Company.objects.create(name="Alpha Corp", code="ALPHA")
        self.company_b = Company.objects.create(name="Beta Corp", code="BETA")

        self.ceo = User.objects.create_user(
            email="ceo_t17@alpha.local",
            username="ceo_t17",
            full_name="Alpha CEO",
            password="Password123!",
            is_superuser=True,
            is_staff=True,
        )
        self.pm_a = User.objects.create_user(
            email="pm_t17@alpha.local",
            username="pm_t17",
            full_name="Alpha PM",
            password="Password123!",
        )
        self.emp_a = User.objects.create_user(
            email="emp_t17@alpha.local",
            username="emp_t17",
            full_name="Alpha Engineer",
            password="Password123!",
        )
        self.pm_b = User.objects.create_user(
            email="pm_t17@beta.local",
            username="pm_b_t17",
            full_name="Beta PM",
            password="Password123!",
        )

        for user, company, role in [
            (self.ceo, self.company_a, self.role_ceo),
            (self.pm_a, self.company_a, self.role_pm),
            (self.emp_a, self.company_a, self.role_emp),
            (self.pm_b, self.company_b, self.role_pm),
        ]:
            cm = CompanyMembership.objects.create(company=company, user=user, status="Active")
            MembershipRole.objects.create(membership=cm, role=role)

        self.dept_a = Department.objects.create(company=self.company_a, name="Engineering", code="ENG-A")
        self.team_a = Team.objects.create(department=self.dept_a, name="Platform", code="PLAT-A")
        TeamMembership.objects.create(team=self.team_a, user=self.emp_a, is_leader=False, status="Active")

    def test_phase_milestone_task_dependency_and_rollup_lifecycle(self):
        self.client.force_authenticate(user=self.pm_a)

        # 1. Create active project with PM, dept, team, members
        proj_resp = self.client.post(
            "/api/projects/",
            {
                "company": str(self.company_a.id),
                "project_manager": str(self.pm_a.id),
                "name": "Integrated Delivery Program",
                "code": "PRJ-T17-01",
                "status": "Active",
                "priority": "High",
                "start_date": str(self.today),
                "planned_completion_date": str(self.today + timedelta(days=30)),
                "department_ids": [str(self.dept_a.id)],
                "team_ids": [str(self.team_a.id)],
                "member_ids": [str(self.pm_a.id), str(self.emp_a.id)],
            },
            format="json",
        )
        self.assertEqual(proj_resp.status_code, status.HTTP_201_CREATED, proj_resp.content)
        project_id = proj_resp.json()["id"]

        # 2. Create 2 Phases and 2 Milestones (M2 depends on M1)
        ph1 = self.client.post(
            "/api/projects/phases/",
            {"project": project_id, "name": "Phase 1: Build", "owner": str(self.pm_a.id)},
            format="json",
        ).json()
        ph2 = self.client.post(
            "/api/projects/phases/",
            {"project": project_id, "name": "Phase 2: Release", "owner": str(self.pm_a.id)},
            format="json",
        ).json()

        m1 = self.client.post(
            "/api/projects/milestones/",
            {
                "project": project_id,
                "title": "M1: Core Complete",
                "target_date": str(self.today + timedelta(days=10)),
                "owner": str(self.pm_a.id),
                "approval_required": True,
            },
            format="json",
        ).json()
        m2 = self.client.post(
            "/api/projects/milestones/",
            {
                "project": project_id,
                "title": "M2: Go-Live",
                "target_date": str(self.today + timedelta(days=20)),
                "owner": str(self.pm_a.id),
                "approval_required": False,
            },
            format="json",
        ).json()

        # Link M2 -> depends on M1
        dep_ok = self.client.post(
            "/api/projects/milestone-dependencies/",
            {"milestone": m2["id"], "depends_on_milestone": m1["id"]},
            format="json",
        )
        self.assertEqual(dep_ok.status_code, status.HTTP_201_CREATED)

        # Circular M1 -> M2 rejected
        dep_cycle = self.client.post(
            "/api/projects/milestone-dependencies/",
            {"milestone": m1["id"], "depends_on_milestone": m2["id"]},
            format="json",
        )
        self.assertEqual(dep_cycle.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Create Tasks linked to Phases and Milestones, with T2 FS-dependent on T1
        t1 = self.client.post(
            "/api/tasks/",
            {
                "project": project_id,
                "phase": ph1["id"],
                "department": str(self.dept_a.id),
                "team": str(self.team_a.id),
                "title": "Build Core API",
                "task_owner": str(self.emp_a.id),
                "priority": "High",
                "status": "In Progress",
                "estimated_effort": "8.00",
                "due_date": str(self.today + timedelta(days=5)),
                "approval_required": False,
            },
            format="json",
        ).json()
        t2 = self.client.post(
            "/api/tasks/",
            {
                "project": project_id,
                "phase": ph2["id"],
                "department": str(self.dept_a.id),
                "team": str(self.team_a.id),
                "title": "Deploy Production",
                "task_owner": str(self.emp_a.id),
                "priority": "High",
                "status": "To Do",
                "estimated_effort": "4.00",
                "due_date": str(self.today + timedelta(days=15)),
                "approval_required": False,
            },
            format="json",
        ).json()

        # Link tasks to milestones via MilestoneTask
        self.assertEqual(
            self.client.post(
                "/api/projects/milestone-tasks/",
                {"milestone": m1["id"], "task": t1["id"]},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self.client.post(
                "/api/projects/milestone-tasks/",
                {"milestone": m2["id"], "task": t2["id"]},
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )

        # FS dependency: T2 depends on T1
        self.assertEqual(
            self.client.post(
                "/api/tasks/dependencies/",
                {
                    "task": t2["id"],
                    "depends_on_task": t1["id"],
                    "dependency_type": "Finish-to-Start",
                },
                format="json",
            ).status_code,
            status.HTTP_201_CREATED,
        )

        # 4. Cannot complete M2 while M1 is incomplete (both action and PATCH blocked)
        self.assertEqual(
            self.client.post(f"/api/projects/milestones/{m2['id']}/complete/").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/projects/milestones/{m2['id']}/",
                {"status": "Completed"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # 5. Progress update on T1 (50%) rolls up Phase 1, M1, and Project progress
        self.client.force_authenticate(user=self.emp_a)
        prog_res = self.client.post(
            "/api/tracking/progress-updates/",
            {
                "task": t1["id"],
                "progress_percentage": 50,
                "work_completed": "Implemented models and serializers",
                "time_spent_minutes": 120,
            },
            format="json",
        )
        self.assertEqual(prog_res.status_code, status.HTTP_201_CREATED)

        ph1_obj = ProjectPhase.objects.get(id=ph1["id"])
        m1_obj = ProjectMilestone.objects.get(id=m1["id"])
        proj_obj = Project.objects.get(id=project_id)
        self.assertEqual(ph1_obj.progress, Decimal("50.00"))
        self.assertEqual(ph1_obj.status, ProjectPhase.Status.IN_PROGRESS)
        self.assertEqual(m1_obj.progress, Decimal("50.00"))
        self.assertEqual(m1_obj.status, ProjectMilestone.Status.IN_PROGRESS)
        self.assertEqual(proj_obj.progress, Decimal("25.00"))

        # 6. Complete T1 -> Phase 1 auto-completes; M1 reaches 100% and awaits PM completion sign-off
        self.client.force_authenticate(user=self.pm_a)
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t1['id']}/transition-status/",
                {"status": "Completed"},
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        ph1_obj.refresh_from_db()
        m1_obj.refresh_from_db()
        self.assertEqual(ph1_obj.status, ProjectPhase.Status.COMPLETED)
        self.assertEqual(ph1_obj.progress, Decimal("100.00"))
        self.assertEqual(m1_obj.progress, Decimal("100.00"))
        # Approval required -> still not auto-completed until explicit sign-off
        self.assertNotEqual(m1_obj.status, ProjectMilestone.Status.COMPLETED)

        comp_m1 = self.client.post(f"/api/projects/milestones/{m1['id']}/complete/")
        self.assertEqual(comp_m1.status_code, status.HTTP_200_OK)
        self.assertEqual(comp_m1.json()["status"], "Completed")
        self.assertTrue(
            Notification.objects.filter(
                related_project_id=project_id,
                notification_type="MILESTONE_COMPLETED",
            ).exists()
        )

        # 7. Now T2 is unblocked -> start & complete T2 -> M2 auto-completes (approval_required=False)
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t2['id']}/transition-status/",
                {"status": "In Progress"},
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t2['id']}/transition-status/",
                {"status": "Completed"},
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        m2_obj = ProjectMilestone.objects.get(id=m2["id"])
        self.assertEqual(m2_obj.status, ProjectMilestone.Status.COMPLETED)
        self.assertEqual(m2_obj.progress, Decimal("100.00"))

        # 8. Log a risk + issue and verify enriched ProjectDetail dashboard_summary
        Risk.objects.create(
            project_id=project_id,
            title="Cutover Window Risk",
            probability="Medium",
            impact="High",
            severity="High",
            status="Open",
        )
        Issue.objects.create(
            project_id=project_id,
            task_id=t2["id"],
            title="DNS propagation lag",
            priority="Medium",
            reported_by=self.pm_a,
            status="Open",
        )

        detail = self.client.get(f"/api/projects/{project_id}/").json()
        summary = detail["dashboard_summary"]
        self.assertEqual(summary["phases_total"], 2)
        self.assertEqual(summary["phases_completed"], 2)
        self.assertEqual(summary["milestones_total"], 2)
        self.assertEqual(summary["milestones_completed"], 2)
        self.assertEqual(summary["total_estimated_effort"], 12.0)
        self.assertEqual(summary["open_risks_count"], 1)
        self.assertEqual(summary["open_issues_count"], 1)

        # 9. Close project cleanly
        close_resp = self.client.post(
            f"/api/projects/{project_id}/close_project/",
            {"closure_remarks": "All phases, milestones, and deliverables verified."},
            format="json",
        )
        self.assertEqual(close_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(close_resp.json()["status"], "Completed")

    def test_live_timer_one_active_rule_time_approval_notifications_and_reports(self):
        project = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm_a,
            name="Timer & Reports Project",
            code="PRJ-T17-02",
            status=Project.Status.ACTIVE,
        )
        task_overdue = Task.objects.create(
            project=project,
            title="Overdue Integration Task",
            created_by=self.pm_a,
            task_owner=self.emp_a,
            status=Task.Status.IN_PROGRESS,
            priority=Task.Priority.CRITICAL,
            due_date=self.today - timedelta(days=3),
        )
        task_ok = Task.objects.create(
            project=project,
            title="Upcoming Task",
            created_by=self.pm_a,
            task_owner=self.emp_a,
            status=Task.Status.TO_DO,
            priority=Task.Priority.MEDIUM,
            due_date=self.today + timedelta(days=5),
        )

        # Employee starts timer on task_overdue
        self.client.force_authenticate(user=self.emp_a)
        start_1 = self.client.post(
            "/api/tracking/time-entries/start-timer/",
            {"task": str(task_overdue.id)},
            format="json",
        )
        self.assertEqual(start_1.status_code, status.HTTP_201_CREATED)
        timer_id = start_1.json()["id"]

        # Second concurrent timer blocked (one active timer per employee)
        start_2 = self.client.post(
            "/api/tracking/time-entries/start-timer/",
            {"task": str(task_ok.id)},
            format="json",
        )
        self.assertEqual(start_2.status_code, status.HTTP_400_BAD_REQUEST)

        # Active timer endpoint returns running timer
        active = self.client.get("/api/tracking/time-entries/active-timer/").json()
        self.assertEqual(active["active_timer"]["id"], timer_id)

        # Stop timer records >= 1 minute
        stopped = self.client.post("/api/tracking/time-entries/stop-timer/")
        self.assertEqual(stopped.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(stopped.json()["ended_at"])
        self.assertGreaterEqual(stopped.json()["duration_minutes"], 1)

        # Employee cannot self-approve time entry -> 403
        self.assertEqual(
            self.client.post(
                f"/api/tracking/time-entries/{timer_id}/approve/",
                {"approval_status": "Approved"},
                format="json",
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        # PM approves time entry -> 200
        self.client.force_authenticate(user=self.pm_a)
        approved = self.client.post(
            f"/api/tracking/time-entries/{timer_id}/approve/",
            {"approval_status": "Approved"},
            format="json",
        )
        self.assertEqual(approved.status_code, status.HTTP_200_OK)
        self.assertEqual(approved.json()["approval_status"], "Approved")

        # Notifications: strict user scoping + mark-all-read
        Notification.objects.create(
            user=self.emp_a,
            notification_type="TASK_ASSIGNED",
            title="For Emp A",
            message="Assigned to you",
            related_task=task_overdue,
            related_project=project,
        )
        Notification.objects.create(
            user=self.pm_a,
            notification_type="TASK_REVIEW",
            title="Private PM Alert",
            message="PM only",
            related_project=project,
        )

        self.client.force_authenticate(user=self.emp_a)
        emp_notifs = self.client.get("/api/notifications/").json()["results"]
        self.assertTrue(all(n["user"] == str(self.emp_a.id) for n in emp_notifs))
        self.assertEqual(
            self.client.get("/api/notifications/unread-count/").json()["unread_count"], 1
        )
        self.assertEqual(
            self.client.post("/api/notifications/mark-all-read/").json()["marked_read"], 1
        )
        self.assertEqual(
            self.client.get("/api/notifications/unread-count/").json()["unread_count"], 0
        )

        # Reports: Employee denied (403); PM gets filtered JSON & CSV
        self.assertEqual(
            self.client.get("/api/reports/tasks/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.client.force_authenticate(user=self.pm_a)
        rep_json = self.client.get(
            f"/api/reports/tasks/?project={project.id}&overdue=true"
        )
        self.assertEqual(rep_json.status_code, status.HTTP_200_OK)
        self.assertEqual(rep_json.json()["count"], 1)
        self.assertEqual(rep_json.json()["results"][0]["title"], "Overdue Integration Task")

        rep_csv = self.client.get(
            f"/api/reports/tasks/?project={project.id}&overdue=true&export=csv"
        )
        self.assertEqual(rep_csv.status_code, status.HTTP_200_OK)
        self.assertIn("text/csv", rep_csv["Content-Type"])
        self.assertIn("Overdue Integration Task", rep_csv.content.decode("utf-8"))
        self.assertNotIn("Upcoming Task", rep_csv.content.decode("utf-8"))

    def test_multi_company_isolation_across_projects_tasks_risks_and_dashboards(self):
        proj_a = Project.objects.create(
            company=self.company_a,
            project_manager=self.pm_a,
            name="Alpha Secret Project",
            code="ALPHA-1",
            status=Project.Status.ACTIVE,
        )
        proj_b = Project.objects.create(
            company=self.company_b,
            project_manager=self.pm_b,
            name="Beta Secret Project",
            code="BETA-1",
            status=Project.Status.ACTIVE,
        )
        Task.objects.create(project=proj_a, title="Alpha Task", created_by=self.pm_a)
        Task.objects.create(project=proj_b, title="Beta Task", created_by=self.pm_b)
        Risk.objects.create(project=proj_a, title="Alpha Risk", probability="Low", impact="Low", severity="Low")
        Risk.objects.create(project=proj_b, title="Beta Risk", probability="High", impact="High", severity="High")

        # PM A sees ONLY Alpha data
        self.client.force_authenticate(user=self.pm_a)
        proj_codes = [p["code"] for p in self.client.get("/api/projects/").json()["results"]]
        self.assertEqual(proj_codes, ["ALPHA-1"])

        task_titles = [t["title"] for t in self.client.get("/api/tasks/").json()["results"]]
        self.assertEqual(task_titles, ["Alpha Task"])

        risk_titles = [r["title"] for r in self.client.get("/api/risks/risks/").json()["results"]]
        self.assertEqual(risk_titles, ["Alpha Risk"])

        # Direct retrieval of Company B project by PM A -> 404 (company-scoped queryset)
        self.assertEqual(
            self.client.get(f"/api/projects/{proj_b.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

        # CEO sees both companies and can drill down cleanly
        self.client.force_authenticate(user=self.ceo)
        ceo_all = self.client.get("/api/dashboards/ceo/").json()
        self.assertEqual(ceo_all["enterprise_kpis"]["total_companies"], 2)
        self.assertEqual(ceo_all["enterprise_kpis"]["total_projects"], 2)

        ceo_alpha = self.client.get(f"/api/dashboards/ceo/?company={self.company_a.id}").json()
        self.assertEqual(ceo_alpha["enterprise_kpis"]["total_projects"], 1)
        self.assertEqual(ceo_alpha["project_health"][0]["code"], "ALPHA-1")

