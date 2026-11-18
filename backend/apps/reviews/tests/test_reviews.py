from datetime import timedelta
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import Notification, NotificationDelivery
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import Project
from apps.reviews.models import TaskReview
from apps.tasks.models import CommentAttachment, Task, TaskAttachment, TaskComment


class ReviewApprovalWorkflowTestCase(APITestCase):
    """
    Task #14: Implement review, approval and correction workflow (Approval flow working).
    Validates SRS §7.3, §13.1, §14.1–§14.4, §20.2, §22 (#5, #7, #17, #20), §27.3 & DB Design §4.6, §5, §8.
    """

    def setUp(self):
        self.pm = User.objects.create_user(
            email="pm_rev@acme.com",
            username="pm_rev",
            full_name="PM Reviewer",
            designation="Project Manager",
            password="Password123!",
        )
        self.head = User.objects.create_user(
            email="head_rev@acme.com",
            username="head_rev",
            full_name="Dept Head Reviewer",
            designation="Department Head",
            password="Password123!",
        )
        self.lead = User.objects.create_user(
            email="lead_rev@acme.com",
            username="lead_rev",
            full_name="Team Lead Reviewer",
            designation="Team Leader",
            password="Password123!",
        )
        self.dev = User.objects.create_user(
            email="dev_rev@acme.com",
            username="dev_rev",
            full_name="Dev Assignee",
            designation="Software Engineer",
            password="Password123!",
        )
        self.outsider = User.objects.create_user(
            email="outsider_rev@other.com",
            username="outsider_rev",
            full_name="Outsider User",
            password="Password123!",
        )

        self.company = Company.objects.create(name="Review Corp", code="REV-CO")
        self.other_company = Company.objects.create(name="Other Corp", code="OTH-CO")

        for u in (self.pm, self.head, self.lead, self.dev):
            CompanyMembership.objects.create(company=self.company, user=u, status="Active")
        CompanyMembership.objects.create(company=self.other_company, user=self.outsider, status="Active")

        self.dept = Department.objects.create(company=self.company, name="Engineering", code="ENG-R")
        DepartmentMembership.objects.create(
            department=self.dept,
            user=self.head,
            is_head=True,
            start_date=timezone.now().date(),
            status="Active",
        )
        self.team = Team.objects.create(department=self.dept, name="Backend Team", code="BE-R")
        TeamMembership.objects.create(
            team=self.team, user=self.lead, is_leader=True, status="Active"
        )
        TeamMembership.objects.create(
            team=self.team, user=self.dev, is_leader=False, status="Active"
        )

        self.project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="Review Workflow Platform",
            code="PRJ-REV-01",
            status=Project.Status.ACTIVE,
        )

    def test_configurable_multi_level_approval_chain_and_completion_rules(self):
        # 1. Configure company priority-based approval chains (SRS §14.1)
        self.client.force_authenticate(user=self.pm)
        cfg_res = self.client.post(
            "/api/reviews/approval-chain/",
            {
                "scope": "company",
                "company": str(self.company.id),
                "levels": ["Reviewer"],
                "priority_chains": {
                    "High": ["Team Leader", "Project Manager"],
                    "Low": ["Reviewer"],
                },
                "escalation_hours": 24,
                "allow_reviewer_reassignment": True,
            },
            format="json",
        )
        self.assertEqual(cfg_res.status_code, status.HTTP_200_OK, cfg_res.content)

        # High priority task -> requires Team Leader -> Project Manager
        high_task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="High Priority Payment Auth",
            created_by=self.pm,
            task_owner=self.dev,
            priority=Task.Priority.HIGH,
            status=Task.Status.IN_PROGRESS,
            approval_required=True,
        )

        # Low priority task -> only requires single Reviewer
        low_task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="Low Priority Copy Edit",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.lead,
            priority=Task.Priority.LOW,
            status=Task.Status.IN_PROGRESS,
            approval_required=True,
        )

        # Verify Low task has 1 step, High task has 2 steps
        low_chain_res = self.client.get(f"/api/tasks/{low_task.id}/review-chain/")
        self.assertEqual(low_chain_res.status_code, 200)
        self.assertEqual(low_chain_res.data["approval_chain"]["total_steps"], 1)

        # 2. Dev submits High task for review -> auto-assigns Step 1 (Team Leader) and notifies Lead
        self.client.force_authenticate(user=self.dev)
        sub_res = self.client.post(
            f"/api/tasks/{high_task.id}/submit-for-review/",
            {"notes": "Unit tests green and ready for TL review."},
            format="json",
        )
        self.assertEqual(sub_res.status_code, 200, sub_res.content)
        high_task.refresh_from_db()
        self.assertEqual(high_task.status, Task.Status.IN_REVIEW)
        self.assertEqual(high_task.reviewer_id, self.lead.id)
        self.assertTrue(
            Notification.objects.filter(
                user=self.lead, notification_type="REVIEW_REQUESTED", related_task=high_task
            ).exists()
        )
        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification__related_task=high_task, channel="In-App", delivery_status="Sent"
            ).exists()
        )

        # 3. Unauthorized Employee (dev) cannot approve own work -> 403 Forbidden
        dev_rev = self.client.post(
            "/api/reviews/submit-decision/",
            {"task": str(high_task.id), "decision": "Approved", "comments": "Self approve"},
            format="json",
        )
        self.assertEqual(dev_rev.status_code, status.HTTP_403_FORBIDDEN)

        # 4. Step 2 (PM) cannot skip Step 1 while Team Leader step is pending -> 403 Forbidden
        self.client.force_authenticate(user=self.pm)
        pm_skip = self.client.post(
            "/api/reviews/submit-decision/",
            {"task": str(high_task.id), "decision": "Approved", "comments": "Skipping TL"},
            format="json",
        )
        self.assertEqual(pm_skip.status_code, status.HTTP_403_FORBIDDEN)

        # 5. Step 1 (Team Leader) approves -> stays In Review and forwards to Step 2 (PM)
        self.client.force_authenticate(user=self.lead)
        step1_res = self.client.post(
            f"/api/tasks/{high_task.id}/reviews/",
            {"decision": "Approved", "comments": "Code quality and logic look solid."},
            format="json",
        )
        self.assertEqual(step1_res.status_code, status.HTTP_201_CREATED, step1_res.content)
        high_task.refresh_from_db()
        self.assertEqual(high_task.status, Task.Status.IN_REVIEW)
        self.assertEqual(high_task.reviewer_id, self.pm.id)
        self.assertTrue(
            Notification.objects.filter(
                user=self.pm, notification_type="REVIEW_REQUESTED", related_task=high_task
            ).exists()
        )

        # Cannot transition directly to Completed while Step 2 is still pending!
        bad_comp = self.client.post(
            f"/api/tasks/{high_task.id}/transition-status/",
            {"status": "Completed"},
            format="json",
        )
        self.assertEqual(bad_comp.status_code, status.HTTP_400_BAD_REQUEST)

        # 6. Step 2 (PM) gives final approval -> status becomes Approved -> Completed now allowed!
        self.client.force_authenticate(user=self.pm)
        step2_res = self.client.post(
            "/api/reviews/submit-decision/",
            {"task": str(high_task.id), "decision": "Approved", "comments": "Approved for release."},
            format="json",
        )
        self.assertEqual(step2_res.status_code, status.HTTP_201_CREATED, step2_res.content)
        high_task.refresh_from_db()
        self.assertEqual(high_task.status, Task.Status.APPROVED)
        self.assertTrue(step2_res.data["approval_chain"]["is_approval_satisfied"])
        self.assertTrue(
            Notification.objects.filter(
                user=self.dev, notification_type="TASK_APPROVED", related_task=high_task
            ).exists()
        )

        ok_comp = self.client.post(
            f"/api/tasks/{high_task.id}/transition-status/",
            {"status": "Completed"},
            format="json",
        )
        self.assertEqual(ok_comp.status_code, status.HTTP_200_OK, ok_comp.content)
        self.assertEqual(ok_comp.data["status"], "Completed")

    def test_correction_rejection_attachments_resubmission_and_immutable_history(self):
        self.client.force_authenticate(user=self.pm)
        task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="Implement Audit Export PDF",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.pm,
            priority=Task.Priority.MEDIUM,
            status=Task.Status.IN_REVIEW,
            approval_required=True,
        )

        # 1. Requesting Changes without correction_reason must fail (DB Design §5 & SRS §14.2)
        missing_reason = self.client.post(
            "/api/reviews/",
            {"task": str(task.id), "decision": "Changes Requested", "comments": "Needs fix"},
            format="json",
        )
        self.assertEqual(missing_reason.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. Request Changes WITH correction_reason + uploaded reference document
        ref_doc = SimpleUploadedFile(
            "correction_checklist.pdf",
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF",
            content_type="application/pdf",
        )
        cr_res = self.client.post(
            "/api/reviews/submit-decision/",
            {
                "task": str(task.id),
                "decision": "Changes Requested",
                "comments": "Please review section 3 formatting guidelines.",
                "correction_reason": "Missing company header logo and UUID column formatting",
                "files": [ref_doc],
            },
            format="multipart",
        )
        self.assertEqual(cr_res.status_code, status.HTTP_201_CREATED, cr_res.content)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CHANGES_REQUESTED)
        self.assertEqual(cr_res.data["review_round"], 1)
        self.assertEqual(len(cr_res.data["reference_attachments"]), 1)
        self.assertEqual(
            cr_res.data["reference_attachments"][0]["file_name"], "correction_checklist.pdf"
        )
        self.assertTrue(TaskAttachment.objects.filter(task=task).exists())
        self.assertTrue(CommentAttachment.objects.filter(comment__task=task).exists())
        self.assertTrue(
            Notification.objects.filter(
                user=self.dev, notification_type="CHANGES_REQUESTED", related_task=task
            ).exists()
        )

        # 3. Historical review records cannot be deleted (DB Design §5)
        del_rev = self.client.delete(f"/api/reviews/{cr_res.data['id']}/")
        self.assertEqual(del_rev.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(TaskReview.objects.filter(id=cr_res.data["id"]).exists())

        # 4. Dev resubmits task after fixing -> PM rejects in Round 2 -> Dev fixes & resubmits -> Round 3 Approved
        self.client.force_authenticate(user=self.dev)
        resub = self.client.post(
            "/api/reviews/submit/",
            {"task": str(task.id), "notes": "Updated header and UUID alignment"},
            format="json",
        )
        self.assertEqual(resub.status_code, status.HTTP_200_OK)
        self.assertEqual(resub.data["status"], "In Review")

        self.client.force_authenticate(user=self.pm)
        rej_res = self.client.post(
            "/api/reviews/submit-decision/",
            {
                "task": str(task.id),
                "decision": "Rejected",
                "correction_reason": "PDF export crashes on empty dataset",
                "comments": "Handled zero-row edge case improperly",
            },
            format="json",
        )
        self.assertEqual(rej_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(rej_res.data["review_round"], 2)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.CHANGES_REQUESTED)
        self.assertTrue(
            Notification.objects.filter(
                user=self.dev, notification_type="REVIEW_REJECTED", related_task=task
            ).exists()
        )

        # Resubmit & approve Round 3
        self.client.force_authenticate(user=self.dev)
        self.client.post(f"/api/tasks/{task.id}/submit-for-review/", {"notes": "Fixed empty guard"}, format="json")
        self.client.force_authenticate(user=self.pm)
        app_res = self.client.post(
            f"/api/tasks/{task.id}/reviews/",
            {"decision": "Approved", "comments": "Empty dataset handled cleanly."},
            format="json",
        )
        self.assertEqual(app_res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(app_res.data["review_round"], 3)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.APPROVED)
        self.assertEqual(task.reviews.count(), 3)

    def test_reviewer_reassignment_sla_escalation_and_review_queue_analytics(self):
        self.client.force_authenticate(user=self.pm)
        task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="Stale Review Deliverable",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.lead,
            priority=Task.Priority.HIGH,
            status=Task.Status.IN_REVIEW,
            approval_required=True,
        )
        sub_comment = TaskComment.objects.create(
            task=task,
            author=self.dev,
            content="[Submitted for Review] In Progress → In Review",
        )
        # Backdate submission timestamp by 72 hours to trigger SLA escalation (> 48h default)
        old_ts = timezone.now() - timedelta(hours=72)
        TaskComment.objects.filter(id=sub_comment.id).update(created_at=old_ts)
        Task.objects.filter(id=task.id).update(updated_at=old_ts)

        # 1. Cross-company outsider reassignment must fail
        bad_reassign = self.client.post(
            f"/api/tasks/{task.id}/reassign-reviewer/",
            {"new_reviewer": str(self.outsider.id), "reason": "External review"},
            format="json",
        )
        self.assertEqual(bad_reassign.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. Unauthorized employee cannot reassign reviewer
        self.client.force_authenticate(user=self.dev)
        unauth_reassign = self.client.post(
            "/api/reviews/reassign-reviewer/",
            {"task": str(task.id), "new_reviewer": str(self.head.id)},
            format="json",
        )
        self.assertEqual(unauth_reassign.status_code, status.HTTP_403_FORBIDDEN)

        # 3. Authorized PM reassigns reviewer to Dept Head
        self.client.force_authenticate(user=self.pm)
        ok_reassign = self.client.post(
            f"/api/tasks/{task.id}/reassign-reviewer/",
            {"new_reviewer": str(self.head.id), "reason": "Lead on leave, routing to Dept Head"},
            format="json",
        )
        self.assertEqual(ok_reassign.status_code, status.HTTP_200_OK, ok_reassign.content)
        task.refresh_from_db()
        self.assertEqual(task.reviewer_id, self.head.id)
        self.assertTrue(
            Notification.objects.filter(
                user=self.head, notification_type="REVIEWER_REASSIGNED", related_task=task
            ).exists()
        )

        # 4. Run SLA escalation check -> triggers REVIEW_ESCALATION notifications to reviewer & PM
        esc_res = self.client.post(
            "/api/reviews/escalate-overdue/",
            {"company": str(self.company.id), "trigger_notifications": True},
            format="json",
        )
        self.assertEqual(esc_res.status_code, status.HTTP_200_OK, esc_res.content)
        self.assertGreaterEqual(esc_res.data["overdue_count"], 1)
        self.assertGreaterEqual(esc_res.data["notifications_sent"], 2)
        self.assertTrue(
            Notification.objects.filter(
                user=self.head, notification_type="REVIEW_ESCALATION", related_task=task
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                user=self.pm, notification_type="REVIEW_ESCALATION", related_task=task
            ).exists()
        )

        # 5. Review Queue & Summary endpoints reflect pending SLA item cleanly
        q_res = self.client.get(f"/api/reviews/queue/?company={self.company.id}&queue_tab=pending&overdue_only=true")
        self.assertEqual(q_res.status_code, status.HTTP_200_OK)
        q_items = q_res.data["results"] if "results" in q_res.data else q_res.data
        self.assertEqual(len(q_items), 1)
        self.assertTrue(q_items[0]["sla"]["is_review_overdue"])
        self.assertEqual(q_items[0]["reviewer_name"], self.head.full_name)

        sum_res = self.client.get(f"/api/reviews/summary/?company={self.company.id}")
        self.assertEqual(sum_res.status_code, status.HTTP_200_OK)
        self.assertEqual(sum_res.data["pending_review_count"], 1)
        self.assertEqual(sum_res.data["overdue_escalations_count"], 1)
