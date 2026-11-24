from datetime import date, timedelta
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import Notification, NotificationDelivery
from apps.organization.models import Department, Team
from apps.projects.models import Project
from apps.tasks.models import Task, TaskComment, TaskDependency
from apps.tasks.services import (
    compute_task_schedule_metrics,
    count_working_days_between,
    get_company_working_calendar,
)


class TaskDependenciesAndOverdueTestCase(APITestCase):
    """
    Comprehensive tests for Task #15 (SRS §7.9, §7.10, §12.3, §12.4, §13.1, §20.1, §20.2, §22, §27.3):
    - All 4 dependency types (FS, SS, FF, SF) enforcement during status transitions
    - Authorized dependency override with mandatory override_reason & audit comment
    - Direct, cross-project, self, and transitive multi-hop cycle rejection (A -> B -> C -> A)
    - Downstream transitive impact analysis, schedule overlap detection, and manager delay notifications
    - Company working-calendar working-day overdue calculation (skipping weekends & holidays)
    - Overdue severity tiers, due_soon_days configuration, overdue-summary, dependency-graph,
      check-deadlines with deduplication + NotificationDelivery, filters, dashboard, and CSV report
    """

    def setUp(self):
        self.pm = User.objects.create_user(
            email="pm_dep@example.com",
            username="pm_dep",
            full_name="Priya Manager",
            password="Password123!",
        )
        self.dev1 = User.objects.create_user(
            email="dev1_dep@example.com",
            username="dev1_dep",
            full_name="Arjun Dev",
            password="Password123!",
        )
        self.dev2 = User.objects.create_user(
            email="dev2_dep@example.com",
            username="dev2_dep",
            full_name="Meera Dev",
            password="Password123!",
        )
        self.company = Company.objects.create(
            name="Acme Dependency Corp",
            code="ACME-DEP",
            working_calendar={
                "working_days": [0, 1, 2, 3, 4],  # Mon-Fri
                "holidays": [],
                "hours_per_day": 8,
                "due_soon_days": 3,
                "deadline_reminder_days": 2,
            },
        )
        for u in (self.pm, self.dev1, self.dev2):
            CompanyMembership.objects.create(company=self.company, user=u, status="Active")

        self.dept = Department.objects.create(company=self.company, name="Engineering", code="ENG")
        self.team = Team.objects.create(department=self.dept, name="Core Team", code="CORE")

        self.project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="Platform Rollout",
            code="PRJ-ROLL",
            status=Project.Status.ACTIVE,
        )
        self.other_project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="Other Project",
            code="PRJ-OTH",
            status=Project.Status.ACTIVE,
        )
        self.client.force_authenticate(user=self.pm)

    def test_dependency_validation_self_cross_project_and_multi_hop_cycle(self):
        t_a = Task.objects.create(project=self.project, title="Task A", created_by=self.pm, task_owner=self.dev1)
        t_b = Task.objects.create(project=self.project, title="Task B", created_by=self.pm, task_owner=self.dev1)
        t_c = Task.objects.create(project=self.project, title="Task C", created_by=self.pm, task_owner=self.dev2)
        t_other = Task.objects.create(project=self.other_project, title="Other Task", created_by=self.pm)

        # Self-dependency rejected
        r_self = self.client.post(
            "/api/tasks/dependencies/",
            {"task": str(t_a.id), "depends_on_task": str(t_a.id)},
            format="json",
        )
        self.assertEqual(r_self.status_code, 400)

        # Cross-project dependency rejected
        r_cross = self.client.post(
            "/api/tasks/dependencies/",
            {"task": str(t_a.id), "depends_on_task": str(t_other.id)},
            format="json",
        )
        self.assertEqual(r_cross.status_code, 400)

        # Valid chain: B depends on A, C depends on B
        self.assertEqual(
            self.client.post(
                "/api/tasks/dependencies/",
                {"task": str(t_b.id), "depends_on_task": str(t_a.id), "dependency_type": "Finish-to-Start"},
                format="json",
            ).status_code,
            201,
        )
        self.assertEqual(
            self.client.post(
                "/api/tasks/dependencies/",
                {"task": str(t_c.id), "depends_on_task": str(t_b.id), "dependency_type": "Finish-to-Start"},
                format="json",
            ).status_code,
            201,
        )

        # Transitive 3-hop cycle: A depends on C -> rejected!
        r_cycle = self.client.post(
            "/api/tasks/dependencies/",
            {"task": str(t_a.id), "depends_on_task": str(t_c.id), "dependency_type": "Finish-to-Start"},
            format="json",
        )
        self.assertEqual(r_cycle.status_code, 400)
        self.assertIn("Circular", str(r_cycle.content))

    def test_all_four_dependency_types_enforcement_and_authorized_override(self):
        # 1. Finish-to-Start (FS): B cannot start until A is Completed
        t_fs_pre = Task.objects.create(
            project=self.project, title="FS Prereq", created_by=self.pm, task_owner=self.dev1, status=Task.Status.IN_PROGRESS
        )
        t_fs_dep = Task.objects.create(
            project=self.project, title="FS Dependent", created_by=self.pm, task_owner=self.dev2, status=Task.Status.TO_DO
        )
        TaskDependency.objects.create(
            task=t_fs_dep, depends_on_task=t_fs_pre, dependency_type=TaskDependency.DependencyType.FINISH_TO_START
        )

        r_block = self.client.post(
            f"/api/tasks/{t_fs_dep.id}/transition-status/",
            {"status": "In Progress"},
            format="json",
        )
        self.assertEqual(r_block.status_code, 400)
        self.assertIn("Finish-to-Start", r_block.data["error"])

        # Complete prerequisite -> dependent can now start
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_fs_pre.id}/transition-status/",
                {"status": "Completed"},
                format="json",
            ).status_code,
            200,
        )
        r_ok = self.client.post(
            f"/api/tasks/{t_fs_dep.id}/transition-status/",
            {"status": "In Progress"},
            format="json",
        )
        self.assertEqual(r_ok.status_code, 200, r_ok.content)

        # 2. Start-to-Start (SS): D cannot start while C is To Do, CAN start once C is In Progress
        t_ss_pre = Task.objects.create(
            project=self.project, title="SS Prereq", created_by=self.pm, task_owner=self.dev1, status=Task.Status.TO_DO
        )
        t_ss_dep = Task.objects.create(
            project=self.project, title="SS Dependent", created_by=self.pm, task_owner=self.dev2, status=Task.Status.TO_DO
        )
        TaskDependency.objects.create(
            task=t_ss_dep, depends_on_task=t_ss_pre, dependency_type=TaskDependency.DependencyType.START_TO_START
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_ss_dep.id}/transition-status/", {"status": "In Progress"}, format="json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_ss_pre.id}/transition-status/", {"status": "In Progress"}, format="json"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_ss_dep.id}/transition-status/", {"status": "In Progress"}, format="json"
            ).status_code,
            200,
        )

        # 3. Finish-to-Finish (FF): F CAN start while E is To Do, CANNOT complete until E is Completed
        t_ff_pre = Task.objects.create(
            project=self.project, title="FF Prereq", created_by=self.pm, task_owner=self.dev1, status=Task.Status.IN_PROGRESS
        )
        t_ff_dep = Task.objects.create(
            project=self.project, title="FF Dependent", created_by=self.pm, task_owner=self.dev2, status=Task.Status.TO_DO
        )
        TaskDependency.objects.create(
            task=t_ff_dep, depends_on_task=t_ff_pre, dependency_type=TaskDependency.DependencyType.FINISH_TO_FINISH
        )
        # Starting is permitted
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_ff_dep.id}/transition-status/", {"status": "In Progress"}, format="json"
            ).status_code,
            200,
        )
        # Completing is blocked while FF prereq is incomplete
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_ff_dep.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            400,
        )
        # Override without reason -> 400
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_ff_dep.id}/transition-status/",
                {"status": "Completed", "override_dependencies": True},
                format="json",
            ).status_code,
            400,
        )
        # Override with reason -> 200 and recorded in audit comment
        r_override = self.client.post(
            f"/api/tasks/{t_ff_dep.id}/transition-status/",
            {
                "status": "Completed",
                "override_dependencies": True,
                "override_reason": "PM approved early sign-off for parallel deliverable",
            },
            format="json",
        )
        self.assertEqual(r_override.status_code, 200, r_override.content)
        self.assertTrue(
            TaskComment.objects.filter(
                task=t_ff_dep, content__icontains="PM approved early sign-off"
            ).exists()
        )

        # 4. Start-to-Finish (SF): H CAN start while G is To Do, CANNOT complete until G starts
        t_sf_pre = Task.objects.create(
            project=self.project, title="SF Prereq", created_by=self.pm, task_owner=self.dev1, status=Task.Status.TO_DO
        )
        t_sf_dep = Task.objects.create(
            project=self.project, title="SF Dependent", created_by=self.pm, task_owner=self.dev2, status=Task.Status.IN_PROGRESS
        )
        TaskDependency.objects.create(
            task=t_sf_dep, depends_on_task=t_sf_pre, dependency_type=TaskDependency.DependencyType.START_TO_FINISH
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_sf_dep.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_sf_pre.id}/transition-status/", {"status": "In Progress"}, format="json"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{t_sf_dep.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            200,
        )

    def test_working_calendar_and_holiday_aware_overdue_calculation(self):
        # Mon 2026-11-09 through Mon 2026-11-16 has Sat (14) & Sun (15) off -> 5 working days.
        # Add Wed 2026-11-11 as company holiday -> 4 working days!
        self.company.working_calendar = {
            "working_days": [0, 1, 2, 3, 4],
            "holidays": ["2026-11-11"],
            "hours_per_day": 8,
            "due_soon_days": 4,
            "deadline_reminder_days": 2,
        }
        self.company.save(update_fields=["working_calendar"])

        cal = get_company_working_calendar(self.company)
        work_days = count_working_days_between(
            date(2026, 11, 9),
            date(2026, 11, 16),
            cal["working_days_set"],
            cal["holidays_set"],
        )
        self.assertEqual(work_days, 4)

        overdue_task = Task.objects.create(
            project=self.project,
            title="Holiday-Spanning Overdue Task",
            created_by=self.pm,
            task_owner=self.dev1,
            priority=Task.Priority.HIGH,
            start_date=date(2026, 11, 6),
            due_date=date(2026, 11, 9),
            status=Task.Status.IN_PROGRESS,
        )
        metrics = compute_task_schedule_metrics(
            overdue_task,
            today=date(2026, 11, 16),
            calendar_cfg=cal,
        )
        self.assertTrue(metrics["is_overdue"])
        self.assertEqual(metrics["days_overdue"], 7)
        self.assertEqual(metrics["working_days_overdue"], 4)
        self.assertEqual(metrics["due_status"], "Overdue")
        self.assertEqual(metrics["overdue_severity"], "High")

        # Test working-calendar API GET & POST update
        cal_res = self.client.post(
            "/api/tasks/working-calendar/",
            {
                "company": str(self.company.id),
                "working_days": [0, 1, 2, 3, 4, 5],
                "holidays": ["2026-12-25", "2026-11-11"],
                "hours_per_day": 7.5,
                "due_soon_days": 5,
                "deadline_reminder_days": 3,
            },
            format="json",
        )
        self.assertEqual(cal_res.status_code, 200, cal_res.content)
        self.assertEqual(cal_res.data["working_days"], [0, 1, 2, 3, 4, 5])
        self.assertEqual(cal_res.data["holidays"], ["2026-11-11", "2026-12-25"])
        self.assertEqual(cal_res.data["due_soon_days"], 5)
        self.assertEqual(cal_res.data["deadline_reminder_days"], 3)

    def test_downstream_delay_warnings_reschedule_notifications_and_graph(self):
        today = timezone.now().date()
        t1 = Task.objects.create(
            project=self.project,
            title="Design Schema",
            created_by=self.pm,
            task_owner=self.dev1,
            priority=Task.Priority.CRITICAL,
            status=Task.Status.IN_PROGRESS,
            start_date=today - timedelta(days=5),
            due_date=today - timedelta(days=2),  # Overdue
        )
        t2 = Task.objects.create(
            project=self.project,
            title="Build API Endpoints",
            created_by=self.pm,
            task_owner=self.dev2,
            priority=Task.Priority.HIGH,
            status=Task.Status.TO_DO,
            start_date=today - timedelta(days=3),  # Overlaps t1 due_date
            due_date=today + timedelta(days=2),
        )
        t3 = Task.objects.create(
            project=self.project,
            title="Frontend Integration",
            created_by=self.pm,
            task_owner=self.dev2,
            priority=Task.Priority.MEDIUM,
            status=Task.Status.TO_DO,
            start_date=today + timedelta(days=3),
            due_date=today + timedelta(days=8),
        )
        TaskDependency.objects.create(task=t2, depends_on_task=t1, dependency_type="Finish-to-Start")
        TaskDependency.objects.create(task=t3, depends_on_task=t2, dependency_type="Finish-to-Start")

        # 1. Dependency impact endpoint detects transitive chain t1 -> t2 -> t3 & delay warnings
        imp_res = self.client.get(f"/api/tasks/{t1.id}/dependency-impact/")
        self.assertEqual(imp_res.status_code, 200, imp_res.content)
        self.assertEqual(imp_res.data["downstream_direct_count"], 1)
        self.assertEqual(imp_res.data["downstream_total_count"], 2)
        self.assertTrue(imp_res.data["is_critical_bottleneck"])
        self.assertGreaterEqual(len(imp_res.data["delay_warnings"]), 2)

        # 2. Moving t1 to Blocked sends DEPENDENCY_DELAY_WARNING to PM & downstream owner (dev2)
        r_blk = self.client.post(
            f"/api/tasks/{t1.id}/transition-status/",
            {"status": "Blocked", "reason": "Waiting on DBA review"},
            format="json",
        )
        self.assertEqual(r_blk.status_code, 200)
        self.assertTrue(
            Notification.objects.filter(
                user=self.pm,
                related_task=t1,
                notification_type="DEPENDENCY_DELAY_WARNING",
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                user=self.dev2,
                related_task=t1,
                notification_type="DEPENDENCY_DELAY_WARNING",
            ).exists()
        )
        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification__related_task=t1,
                notification__notification_type="DEPENDENCY_DELAY_WARNING",
                channel=NotificationDelivery.Channel.IN_APP,
            ).exists()
        )

        # 3. Rescheduling t1 later also triggers DEPENDENCY_DELAY_WARNING
        Notification.objects.filter(notification_type="DEPENDENCY_DELAY_WARNING").delete()
        r_resched = self.client.post(
            f"/api/tasks/{t1.id}/reschedule/",
            {"due_date": str(today + timedelta(days=5)), "reason": "Extended schema work"},
            format="json",
        )
        self.assertEqual(r_resched.status_code, 200, r_resched.content)
        self.assertTrue(
            Notification.objects.filter(
                user=self.pm,
                related_task=t1,
                notification_type="DEPENDENCY_DELAY_WARNING",
            ).exists()
        )

        # 4. Dependency Graph endpoint returns topological levels 0, 1, 2 and schedule conflicts
        graph_res = self.client.get(f"/api/tasks/dependency-graph/?project={self.project.id}")
        self.assertEqual(graph_res.status_code, 200, graph_res.content)
        levels = {n["id"]: n["topological_level"] for n in graph_res.data["nodes"]}
        self.assertEqual(levels[str(t1.id)], 0)
        self.assertEqual(levels[str(t2.id)], 1)
        self.assertEqual(levels[str(t3.id)], 2)
        self.assertEqual(graph_res.data["summary"]["total_dependencies"], 2)
        self.assertGreaterEqual(graph_res.data["summary"]["schedule_conflicts_count"], 1)

        # 5. TaskFilterSet ?dependency_blocked=true matches t2 & t3 (both blocked by incomplete FS prereq), not t1
        filt_res = self.client.get(f"/api/tasks/?project={self.project.id}&dependency_blocked=true")
        self.assertEqual(filt_res.status_code, 200)
        items = filt_res.data["results"] if "results" in filt_res.data else filt_res.data
        blocked_ids = {item["id"] for item in items}
        self.assertIn(str(t2.id), blocked_ids)
        self.assertIn(str(t3.id), blocked_ids)
        self.assertNotIn(str(t1.id), blocked_ids)

    def test_overdue_summary_check_deadlines_deduplication_dashboard_and_csv_report(self):
        today = timezone.now().date()
        t_over = Task.objects.create(
            project=self.project,
            title="Critical Overdue Deployment",
            created_by=self.pm,
            task_owner=self.dev1,
            priority=Task.Priority.CRITICAL,
            status=Task.Status.IN_PROGRESS,
            due_date=today - timedelta(days=4),
        )
        t_soon = Task.objects.create(
            project=self.project,
            title="Upcoming Deliverable",
            created_by=self.pm,
            task_owner=self.dev2,
            priority=Task.Priority.HIGH,
            status=Task.Status.TO_DO,
            due_date=today + timedelta(days=1),
        )
        TaskDependency.objects.create(task=t_soon, depends_on_task=t_over, dependency_type="Finish-to-Start")

        # 1. Overdue Summary endpoint
        sum_res = self.client.get(f"/api/tasks/overdue-summary/?project={self.project.id}")
        self.assertEqual(sum_res.status_code, 200, sum_res.content)
        self.assertEqual(sum_res.data["kpis"]["overdue_count"], 1)
        self.assertEqual(sum_res.data["kpis"]["critical_overdue_count"], 1)
        self.assertEqual(sum_res.data["kpis"]["due_soon_count"], 1)
        self.assertEqual(sum_res.data["kpis"]["dependency_blocked_count"], 1)
        self.assertEqual(sum_res.data["kpis"]["at_risk_dependent_count"], 1)

        # 2. Check Deadlines dispatches TASK_OVERDUE, UPCOMING_DEADLINE, DEPENDENCY_DELAY_WARNING
        chk1 = self.client.post(
            "/api/tasks/check-deadlines/",
            {"project": str(self.project.id)},
            format="json",
        )
        self.assertEqual(chk1.status_code, 200, chk1.content)
        self.assertEqual(chk1.data["overdue_tasks_found"], 1)
        self.assertEqual(chk1.data["upcoming_deadlines_found"], 1)
        self.assertGreater(chk1.data["notifications_created"], 0)
        self.assertTrue(Notification.objects.filter(related_task=t_over, notification_type="TASK_OVERDUE").exists())
        self.assertTrue(Notification.objects.filter(related_task=t_soon, notification_type="UPCOMING_DEADLINE").exists())

        # Running again immediately deduplicates notifications (0 new created)
        chk2 = self.client.post(
            "/api/tasks/check-deadlines/",
            {"project": str(self.project.id)},
            format="json",
        )
        self.assertEqual(chk2.status_code, 200)
        self.assertEqual(chk2.data["notifications_created"], 0)
        self.assertGreater(chk2.data["skipped_duplicates"], 0)

        # 3. Dashboard summary includes overdue, critical overdue, due soon, dependency_blocked
        dash = self.client.get(f"/api/dashboards/summary/?project={self.project.id}")
        self.assertEqual(dash.status_code, 200)
        self.assertEqual(dash.data["tasks"]["overdue_tasks"], 1)
        self.assertEqual(dash.data["tasks"]["critical_overdue_tasks"], 1)
        self.assertEqual(dash.data["tasks"]["due_soon_tasks"], 1)
        self.assertEqual(dash.data["tasks"]["dependency_blocked_tasks"], 1)

        # 4. CSV & JSON report includes overdue & dependency-blocked fields (SRS §27.3)
        csv_res = self.client.get(f"/api/reports/tasks/?company={self.company.id}&format=csv")
        self.assertEqual(csv_res.status_code, 200)
        body = csv_res.content.decode("utf-8")
        self.assertIn("Working Days Overdue", body)
        self.assertIn("Dependency Blocked", body)
        self.assertIn("Critical Overdue Deployment", body)

        json_res = self.client.get(f"/api/reports/tasks/?company={self.company.id}&format=json")
        self.assertEqual(json_res.status_code, 200)
        by_id = {str(r["id"]): r for r in json_res.data["results"]}
        self.assertTrue(by_id[str(t_over.id)]["is_overdue"])
        self.assertEqual(by_id[str(t_over.id)]["overdue_severity"], "Critical")
        self.assertTrue(by_id[str(t_soon.id)]["is_dependency_blocked"])
