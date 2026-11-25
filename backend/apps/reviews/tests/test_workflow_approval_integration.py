from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import Notification, NotificationDelivery
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import MilestoneTask, Project, ProjectMilestone, ProjectPhase
from apps.rbac.models import MembershipRole, Role
from apps.reviews.models import TaskReview
from apps.system_settings.models import WorkflowStatus, WorkflowTransition
from apps.tasks.models import Task, TaskDependency


class WorkflowAndApprovalIntegrationTestCase(APITestCase):
    """
    Task #16: Fix task workflow and approval integration issues (Task workflow stable).
    Regression & integration suite validating cross-module boundaries between:
    - Task status transitions & role-scoped WorkflowTransition rules
    - Multi-level approval chains & reviewer permission guards on transition-status / PATCH
    - TaskReview audit synchronization on workflow approvals/corrections
    - Approval-cycle reset when a Completed approval_required task is reopened
    - Dependency enforcement on submit-for-review & progress-update auto-start/auto-block
    - Reviewer reassignment persisting step override onto Company/Project policy chains
    - Progress update lockout on Completed tasks & clean progress rollup on empty subtasks/project
    """

    def setUp(self):
        self.pm = User.objects.create_user(
            email="pm_wf16@acme.com",
            username="pm_wf16",
            full_name="Priya PM",
            designation="Project Manager",
            password="Password123!",
        )
        self.head = User.objects.create_user(
            email="head_wf16@acme.com",
            username="head_wf16",
            full_name="Vikram Head",
            designation="Department Head",
            password="Password123!",
        )
        self.lead = User.objects.create_user(
            email="lead_wf16@acme.com",
            username="lead_wf16",
            full_name="Rohan Lead",
            designation="Team Leader",
            password="Password123!",
        )
        self.dev = User.objects.create_user(
            email="dev_wf16@acme.com",
            username="dev_wf16",
            full_name="Ananya Dev",
            designation="Software Engineer",
            password="Password123!",
        )

        self.company = Company.objects.create(name="Workflow Corp", code="WF-CO")
        for u in (self.pm, self.head, self.lead, self.dev):
            CompanyMembership.objects.create(company=self.company, user=u, status="Active")

        self.dept = Department.objects.create(company=self.company, name="Platform", code="PLAT")
        DepartmentMembership.objects.create(
            department=self.dept,
            user=self.head,
            is_head=True,
            start_date=timezone.now().date(),
            status="Active",
        )
        self.team = Team.objects.create(department=self.dept, name="Core Team", code="CORE")
        TeamMembership.objects.create(team=self.team, user=self.lead, is_leader=True, status="Active")
        TeamMembership.objects.create(team=self.team, user=self.dev, is_leader=False, status="Active")

        self.project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="Workflow Integration Project",
            code="PRJ-WF16",
            status=Project.Status.ACTIVE,
        )

    def test_transition_status_cannot_bypass_reviewer_permissions_or_multilevel_chain(self):
        # Configure 2-step chain for High tasks: Team Leader -> Project Manager
        self.client.force_authenticate(user=self.pm)
        self.assertEqual(
            self.client.post(
                "/api/reviews/approval-chain/",
                {
                    "scope": "company",
                    "company": str(self.company.id),
                    "levels": ["Reviewer"],
                    "priority_chains": {"High": ["Team Leader", "Project Manager"]},
                },
                format="json",
            ).status_code,
            200,
        )

        task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="Multi-Level Approval Task",
            created_by=self.pm,
            task_owner=self.dev,
            priority=Task.Priority.HIGH,
            status=Task.Status.IN_PROGRESS,
            approval_required=True,
        )

        # Dev moves to In Review -> auto-assigns Step 1 (Team Leader) & creates NotificationDelivery
        self.client.force_authenticate(user=self.dev)
        r_rev = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "In Review"},
            format="json",
        )
        self.assertEqual(r_rev.status_code, 200, r_rev.content)
        task.refresh_from_db()
        self.assertEqual(task.reviewer_id, self.lead.id)
        self.assertTrue(
            NotificationDelivery.objects.filter(
                notification__related_task=task,
                notification__notification_type="REVIEW_REQUESTED",
            ).exists()
        )

        # Dev (non-reviewer) cannot self-approve via transition-status OR PATCH -> 403 Forbidden
        r_self_approve = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Approved"},
            format="json",
        )
        self.assertEqual(r_self_approve.status_code, status.HTTP_403_FORBIDDEN)
        r_patch_approve = self.client.patch(
            f"/api/tasks/{task.id}/",
            {"status": "Approved"},
            format="json",
        )
        self.assertEqual(r_patch_approve.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("Approved", r_rev.data["allowed_transitions"])

        # Even authorized PM cannot jump directly to Approved bypassing 2-step chain -> 400
        self.client.force_authenticate(user=self.pm)
        r_skip = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Approved"},
            format="json",
        )
        self.assertEqual(r_skip.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Multi-level approval chain", r_skip.data["error"])

        # Step 1 (Lead) approves via review API -> forwards to PM; then update submission does NOT rewind to Lead!
        self.client.force_authenticate(user=self.lead)
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(task.id), "decision": "Approved", "comments": "Step 1 OK"},
                format="json",
            ).status_code,
            201,
        )
        task.refresh_from_db()
        self.assertEqual(task.reviewer_id, self.pm.id)

        # Updating submission notes while Step 2 is pending must keep PM (not rewind to Lead)
        self.client.force_authenticate(user=self.dev)
        upd_sub = self.client.post(
            f"/api/tasks/{task.id}/submit-for-review/",
            {"notes": "Added final deployment notes"},
            format="json",
        )
        self.assertEqual(upd_sub.status_code, 200, upd_sub.content)
        task.refresh_from_db()
        self.assertEqual(task.reviewer_id, self.pm.id)

    def test_reopening_completed_task_resets_approval_cycle_and_syncs_audit_reviews(self):
        task = Task.objects.create(
            project=self.project,
            title="Single-Step Audited Task",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.pm,
            priority=Task.Priority.LOW,
            status=Task.Status.IN_REVIEW,
            approval_required=True,
        )

        # Authorized PM approves via transition-status -> automatically creates TaskReview row!
        self.client.force_authenticate(user=self.pm)
        r_app = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Approved", "reason": "Verified deliverable"},
            format="json",
        )
        self.assertEqual(r_app.status_code, 200, r_app.content)
        self.assertEqual(
            TaskReview.objects.filter(task=task, decision=TaskReview.Decision.APPROVED).count(), 1
        )

        # Duplicate approval on already-satisfied Approved task is rejected -> 400
        r_dup = self.client.post(
            "/api/reviews/submit-decision/",
            {"task": str(task.id), "decision": "Approved", "comments": "Double click"},
            format="json",
        )
        self.assertEqual(r_dup.status_code, status.HTTP_400_BAD_REQUEST)

        # Complete the task
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{task.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            200,
        )

        # Cannot submit progress update on Completed task -> 400
        r_prog_done = self.client.post(
            f"/api/tasks/{task.id}/progress-updates/",
            {"progress_percentage": "50.00", "remarks": "Should fail"},
            format="json",
        )
        self.assertEqual(r_prog_done.status_code, status.HTTP_400_BAD_REQUEST)

        # Cannot record review decision on Completed task -> 400
        r_rev_done = self.client.post(
            "/api/reviews/submit-decision/",
            {"task": str(task.id), "decision": "Approved"},
            format="json",
        )
        self.assertEqual(r_rev_done.status_code, status.HTTP_400_BAD_REQUEST)

        # Reopen Completed -> In Progress -> approval cycle MUST reset so direct re-completion is blocked!
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{task.id}/transition-status/",
                {"status": "In Progress", "reason": "Regression found in QA"},
                format="json",
            ).status_code,
            200,
        )
        task.refresh_from_db()
        self.assertIsNone(task.completed_at)

        # Direct jump to Completed without new approval MUST fail (400)
        r_recomp_fail = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Completed"},
            format="json",
        )
        self.assertEqual(r_recomp_fail.status_code, status.HTTP_400_BAD_REQUEST)

        # Submit for review & re-approve -> now Completed succeeds!
        self.assertEqual(
            self.client.post(f"/api/tasks/{task.id}/submit-for-review/", {}, format="json").status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/reviews/submit-decision/",
                {"task": str(task.id), "decision": "Approved", "comments": "QA regression fixed"},
                format="json",
            ).status_code,
            201,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{task.id}/transition-status/", {"status": "Completed"}, format="json"
            ).status_code,
            200,
        )

    def test_dependencies_enforced_on_submit_for_review_and_progress_updates(self):
        self.client.force_authenticate(user=self.dev)
        prereq = Task.objects.create(
            project=self.project,
            title="Prerequisite API Spec",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
        )
        dependent = Task.objects.create(
            project=self.project,
            title="Dependent Frontend Feature",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.pm,
            status=Task.Status.ASSIGNED,
        )
        TaskDependency.objects.create(
            task=dependent,
            depends_on_task=prereq,
            dependency_type=TaskDependency.DependencyType.FINISH_TO_START,
        )

        # 1. Submitting dependent task for review while FS prereq is incomplete is blocked -> 400
        r_sub_blocked = self.client.post(
            f"/api/tasks/{dependent.id}/submit-for-review/",
            {"notes": "Trying to bypass FS dependency"},
            format="json",
        )
        self.assertEqual(r_sub_blocked.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Finish-to-Start", r_sub_blocked.data["error"])

        # 2. Progress update > 0% on dependency-blocked task must NOT auto-transition it to In Progress
        r_prog = self.client.post(
            f"/api/tasks/{dependent.id}/progress-updates/",
            {"progress_percentage": "25.00", "remarks": "Prep notes only"},
            format="json",
        )
        self.assertEqual(r_prog.status_code, status.HTTP_201_CREATED)
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, Task.Status.ASSIGNED)

        # 3. Progress update with mark_task_blocked=True on prereq warns PM of downstream delay!
        r_blk = self.client.post(
            f"/api/tasks/{prereq.id}/progress-updates/",
            {
                "progress_percentage": "40.00",
                "blockers": "Waiting on external OAuth certs",
                "mark_task_blocked": True,
            },
            format="json",
        )
        self.assertEqual(r_blk.status_code, status.HTTP_201_CREATED)
        prereq.refresh_from_db()
        self.assertEqual(prereq.status, Task.Status.BLOCKED)
        self.assertTrue(
            Notification.objects.filter(
                user=self.pm,
                related_task=prereq,
                notification_type="DEPENDENCY_DELAY_WARNING",
            ).exists()
        )

        # 4. Blocked task cannot be submitted for review -> 400
        r_blk_sub = self.client.post(
            f"/api/tasks/{prereq.id}/submit-for-review/", {}, format="json"
        )
        self.assertEqual(r_blk_sub.status_code, status.HTTP_400_BAD_REQUEST)

        # 5. Authorized override_dependencies on submit-for-review works when reason is given
        r_sub_override = self.client.post(
            f"/api/tasks/{dependent.id}/submit-for-review/",
            {
                "override_dependencies": True,
                "override_reason": "Approved mock spec review in parallel",
            },
            format="json",
        )
        self.assertEqual(r_sub_override.status_code, 200, r_sub_override.content)
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, Task.Status.IN_REVIEW)

    def test_reviewer_reassignment_updates_policy_chain_and_role_scoped_transitions_and_rollups(self):
        # 1. Policy chain Step 1 reassignment persists onto task chain so Dept Head is reflected as Step 1
        self.client.force_authenticate(user=self.pm)
        self.client.post(
            "/api/reviews/approval-chain/",
            {
                "scope": "company",
                "company": str(self.company.id),
                "levels": ["Team Leader", "Project Manager"],
            },
            format="json",
        )
        task = Task.objects.create(
            project=self.project,
            department=self.dept,
            team=self.team,
            title="Delegated Multi-Step Review Task",
            created_by=self.pm,
            task_owner=self.dev,
            reviewer=self.lead,
            status=Task.Status.IN_REVIEW,
            approval_required=True,
        )
        r_reass = self.client.post(
            f"/api/tasks/{task.id}/reassign-reviewer/",
            {"new_reviewer": str(self.head.id), "reason": "Lead on PTO"},
            format="json",
        )
        self.assertEqual(r_reass.status_code, 200, r_reass.content)
        self.assertEqual(
            r_reass.data["approval_chain"]["current_reviewer_id"], str(self.head.id)
        )
        self.assertEqual(
            r_reass.data["approval_chain"]["steps"][0]["user_id"], str(self.head.id)
        )

        # 2. Role-scoped WorkflowTransition restricts transition to users holding that Role
        pm_role = Role.objects.create(name="Project Manager", code="PROJECT_MANAGER")
        ws_todo = WorkflowStatus.objects.create(
            company=self.company, entity_type="Task", status_code="To Do", display_name="To Do"
        )
        ws_cancel = WorkflowStatus.objects.create(
            company=self.company, entity_type="Task", status_code="Cancelled", display_name="Cancelled"
        )
        WorkflowTransition.objects.create(
            company=self.company,
            entity_type="Task",
            from_status=ws_todo,
            to_status=ws_cancel,
            role=pm_role,
        )
        role_task = Task.objects.create(
            project=self.project,
            title="Role-Restricted Task",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.TO_DO,
        )
        # Dev does NOT have PM role -> Cancelled not allowed
        self.client.force_authenticate(user=self.dev)
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{role_task.id}/transition-status/", {"status": "Cancelled"}, format="json"
            ).status_code,
            400,
        )
        # PM is project_manager -> Cancelled IS allowed
        self.client.force_authenticate(user=self.pm)
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{role_task.id}/transition-status/", {"status": "Cancelled"}, format="json"
            ).status_code,
            200,
        )

        # 3. Phase & Milestone progress rollup + reset to 0% when all tasks are deleted
        phase = ProjectPhase.objects.create(project=self.project, name="Rollup Phase")
        ms = ProjectMilestone.objects.create(
            project=self.project, title="Rollup MS", target_date=timezone.now().date() + timedelta(days=5)
        )
        # Clear previous tasks to test exact rollup
        Task.objects.filter(project=self.project).delete()
        t_roll = Task.objects.create(
            project=self.project,
            phase=phase,
            title="Rollup Task",
            created_by=self.pm,
            task_owner=self.dev,
            status=Task.Status.IN_PROGRESS,
            progress=Decimal("80.00"),
        )
        MilestoneTask.objects.create(milestone=ms, task=t_roll)
        from apps.projects.services import recalculate_project_progress_service

        recalculate_project_progress_service(project=self.project)
        phase.refresh_from_db()
        ms.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(phase.progress, Decimal("80.00"))
        self.assertEqual(ms.progress, Decimal("80.00"))
        self.assertEqual(self.project.progress, Decimal("80.00"))

        # Delete the only task -> project progress resets cleanly to 0.00 (no stale 80%)
        self.assertEqual(self.client.delete(f"/api/tasks/{t_roll.id}/").status_code, 204)
        self.project.refresh_from_db()
        self.assertEqual(self.project.progress, Decimal("0.00"))

