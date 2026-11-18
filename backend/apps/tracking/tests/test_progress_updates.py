from datetime import date, timedelta
from decimal import Decimal
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import Notification
from apps.organization.models import Department, Team
from apps.projects.models import Project
from apps.risks.models import Blocker
from apps.system_settings.models import SystemSetting
from apps.tasks.models import Attachment, Checklist, ChecklistItem, Task
from apps.tracking.models import ProgressAttachment, ProgressUpdate, TimeEntry


class ProgressUpdatesAndRemarksTestCase(APITestCase):
    """
    Task #12: Implement progress updates and remarks (Progress update flow working).
    Validates SRS §7.5, §7.6, §8.3, §9.1–§9.4, §13.1, §22 (#6, #8, #13) & DB Design §4.5, §5, §7, §8.
    """

    def setUp(self):
        self.pm = User.objects.create_user(
            email="pm_prog@acme.com",
            username="pm_prog",
            full_name="PM Progress",
            password="Password123!",
        )
        self.dev = User.objects.create_user(
            email="dev_prog@acme.com",
            username="dev_prog",
            full_name="Dev Progress",
            password="Password123!",
        )
        self.outsider = User.objects.create_user(
            email="outsider_prog@other.com",
            username="outsider_prog",
            full_name="Outsider User",
            password="Password123!",
        )

        self.company = Company.objects.create(name="Progress Corp", code="PROG-CO")
        self.other_company = Company.objects.create(name="Other Corp", code="OTHER-CO")

        CompanyMembership.objects.create(company=self.company, user=self.pm, status="Active")
        CompanyMembership.objects.create(company=self.company, user=self.dev, status="Active")
        CompanyMembership.objects.create(company=self.other_company, user=self.outsider, status="Active")

        self.dept = Department.objects.create(company=self.company, name="Engineering", code="ENG")
        self.team = Team.objects.create(department=self.dept, name="Backend Team", code="BE")

        self.project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="Progress Tracking Platform",
            code="PRJ-PROG-01",
            status=Project.Status.ACTIVE,
        )
        self.client.force_authenticate(user=self.dev)

    def test_manual_progress_update_remarks_time_entry_and_project_rollup(self):
        task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="Implement OAuth2 Token Refresh",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.pm,
            status=Task.Status.ASSIGNED,
            progress_method=Task.ProgressMethod.MANUAL,
            estimated_effort=Decimal("8.00"),
            due_date=date.today() + timedelta(days=3),
        )
        att = Attachment.objects.create(
            uploaded_by=self.dev,
            file_name="oauth_flow_diagram.png",
            storage_key="uploads/oauth_flow_diagram.png",
            mime_type="image/png",
            file_size_bytes=24500,
        )

        res = self.client.post(
            f"/api/tasks/{task.id}/progress-updates/",
            {
                "progress_percentage": "65.00",
                "work_completed": "Implemented refresh token rotation and Redis blacklist.",
                "remaining_work": "Write integration tests for expired refresh tokens.",
                "remarks": "Token rotation verified locally against staging IdP.",
                "time_spent_minutes": 150,
                "next_action": "Submit PR for security review tomorrow morning.",
                "attachment_ids": [str(att.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(Decimal(str(res.data["progress_percentage"])), Decimal("65.00"))
        self.assertEqual(res.data["author_name"], "Dev Progress")
        self.assertEqual(res.data["remarks"], "Token rotation verified locally against staging IdP.")
        self.assertEqual(len(res.data["attachments"]), 1)
        self.assertTrue(
            ProgressAttachment.objects.filter(
                progress_update_id=res.data["id"], attachment=att
            ).exists()
        )

        # Verify task progress updated, auto-transitioned from Assigned -> In Progress, and rolled up to project
        task.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(task.progress, Decimal("65.00"))
        self.assertEqual(task.status, Task.Status.IN_PROGRESS)
        self.assertEqual(self.project.progress, Decimal("65.00"))

        # Verify TimeEntry was recorded and actual_effort_hours is 2.5h (150 mins)
        self.assertEqual(TimeEntry.objects.filter(task=task, user=self.dev).count(), 1)
        detail_res = self.client.get(f"/api/tasks/{task.id}/")
        self.assertEqual(detail_res.status_code, 200)
        self.assertEqual(float(detail_res.data["actual_effort_hours"]), 2.5)
        self.assertEqual(len(detail_res.data["progress_updates"]), 1)

        # Verify PM received PROGRESS_UPDATED notification
        self.assertTrue(
            Notification.objects.filter(
                user=self.pm,
                notification_type="PROGRESS_UPDATED",
                related_task=task,
            ).exists()
        )

    def test_manual_progress_not_overwritten_by_auto_calculations_and_weighted_subtasks(self):
        """
        SRS §7.6 & DB Design §5:
        1. Manual progress must not be silently overwritten by automatic calculations.
        2. For weighted subtasks: Parent progress = Sum(child progress * child weight) / Sum(child weights).
        """
        manual_parent = Task.objects.create(
            project=self.project,
            title="Manual Parent Task",
            created_by=self.pm,
            task_owner=self.dev,
            progress_method=Task.ProgressMethod.MANUAL,
            progress=Decimal("40.00"),
        )
        # Creating a completed subtask under a MANUAL parent must NOT overwrite the manual 40% progress
        Task.objects.create(
            project=self.project,
            parent_task=manual_parent,
            title="Child of Manual Parent",
            created_by=self.pm,
            status=Task.Status.COMPLETED,
        )
        manual_parent.refresh_from_db()
        self.assertEqual(manual_parent.progress, Decimal("40.00"))

        # Weighted Subtasks Parent: child1 weight=2h (100% done), child2 weight=6h (50% done)
        # Expected weighted progress = (100*2 + 50*6) / (2 + 6) = 500 / 8 = 62.50%
        weighted_parent = Task.objects.create(
            project=self.project,
            title="Weighted Subtasks Parent",
            created_by=self.pm,
            task_owner=self.dev,
            progress_method=Task.ProgressMethod.SUBTASKS,
        )
        Task.objects.create(
            project=self.project,
            parent_task=weighted_parent,
            title="Subtask Weight 2h",
            created_by=self.pm,
            estimated_effort=Decimal("2.00"),
            status=Task.Status.COMPLETED,
        )
        Task.objects.create(
            project=self.project,
            parent_task=weighted_parent,
            title="Subtask Weight 6h",
            created_by=self.pm,
            estimated_effort=Decimal("6.00"),
            status=Task.Status.IN_PROGRESS,
            progress=Decimal("50.00"),
        )
        weighted_parent.refresh_from_db()
        self.assertEqual(weighted_parent.progress, Decimal("62.50"))

        # Switching manual_parent from Manual -> Checklist recalculates from checklist items immediately
        cl = Checklist.objects.create(task=manual_parent, title="Verification", created_by=self.pm)
        ChecklistItem.objects.create(checklist=cl, title="Step 1", is_completed=True)
        ChecklistItem.objects.create(checklist=cl, title="Step 2", is_completed=True)
        ChecklistItem.objects.create(checklist=cl, title="Step 3", is_completed=False)
        ChecklistItem.objects.create(checklist=cl, title="Step 4", is_completed=False)

        patch_res = self.client.patch(
            f"/api/tasks/{manual_parent.id}/",
            {"progress_method": "Checklist"},
            format="json",
        )
        self.assertEqual(patch_res.status_code, 200)
        manual_parent.refresh_from_db()
        self.assertEqual(manual_parent.progress, Decimal("50.00"))

    def test_blocker_reporting_in_progress_update_and_work_log_review_summary(self):
        """
        SRS §9.3 Work Log Review & §9.4 Blocker Reporting:
        - Submitting a progress update with blockers creates a Blocker record, notifies PM,
          and transitions task status to Blocked when mark_task_blocked or workflow setting is active.
        - /api/tracking/progress-updates/summary/ returns manager work log review metrics.
        """
        SystemSetting.objects.create(
            company=self.company,
            setting_key="auto_block_on_blocker",
            setting_value=True,
        )
        delayed_task = Task.objects.create(
            project=self.project,
            title="Legacy Data Migration",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
            estimated_effort=Decimal("10.00"),
            due_date=date.today() - timedelta(days=2),
            approval_required=True,
        )

        res = self.client.post(
            "/api/tracking/progress-updates/",
            {
                "task": str(delayed_task.id),
                "progress_percentage": "35.00",
                "work_completed": "Extracted customer ledger tables.",
                "remaining_work": "Transform currency precision columns.",
                "remarks": "Blocked waiting on DBA firewall rule for staging replica.",
                "blockers": "Staging replica port 5432 blocked by VPC security group.",
                "blocker_category": "Infrastructure",
                "blocker_impact": "Critical",
                "time_spent_minutes": 180,
                "next_action": "Follow up with DevOps lead once firewall rule is applied.",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)

        delayed_task.refresh_from_db()
        self.assertEqual(delayed_task.status, Task.Status.BLOCKED)
        self.assertEqual(delayed_task.progress, Decimal("35.00"))
        self.assertTrue(
            Blocker.objects.filter(
                task=delayed_task,
                category="Infrastructure",
                impact="Critical",
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                user=self.pm,
                notification_type="BLOCKER_REPORTED",
                related_task=delayed_task,
            ).exists()
        )

        # Verify Work Log Review Summary endpoint (SRS §9.3)
        summary_res = self.client.get(
            f"/api/tracking/progress-updates/summary/?project={self.project.id}"
        )
        self.assertEqual(summary_res.status_code, 200)
        summary = summary_res.data
        self.assertEqual(summary["total_updates"], 1)
        self.assertEqual(summary["blockers_reported_count"], 1)
        self.assertEqual(summary["delayed_tasks_metrics"]["delayed_tasks_count"], 1)
        self.assertEqual(summary["delayed_tasks_metrics"]["time_spent_on_delayed_minutes"], 180)
        self.assertEqual(summary["effort_comparison"]["total_estimated_hours"], 10.0)
        self.assertEqual(summary["effort_comparison"]["total_actual_hours"], 3.0)
        self.assertEqual(len(summary["employee_workload"]), 1)
        self.assertEqual(summary["employee_workload"][0]["author_name"], "Dev Progress")

        # Verify cross-company author validation rejects outsider
        bad_res = self.client.post(
            "/api/tracking/progress-updates/",
            {
                "task": str(delayed_task.id),
                "author": str(self.outsider.id),
                "progress_percentage": "50.00",
            },
            format="json",
        )
        self.assertEqual(bad_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ProgressUpdate.objects.filter(task=delayed_task).count(), 1)
