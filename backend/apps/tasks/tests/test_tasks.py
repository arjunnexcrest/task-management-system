import uuid
from decimal import Decimal
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import User
from apps.companies.models import Company
from apps.organization.models import Department, Team
from apps.projects.models import Project
from apps.tasks.models import (
    Checklist,
    ChecklistItem,
    Task,
    TaskAssignment,
    TaskComment,
    TaskDependency,
    TaskTag,
    TaskTagAssignment,
)


class TaskAndAssignmentModelsTestCase(TestCase):
    def setUp(self):
        self.pm = User.objects.create_user(
            email="pm@tasks.com",
            username="pm_tasks",
            full_name="Project Manager",
            password="Password123!",
        )
        self.dev = User.objects.create_user(
            email="dev@tasks.com",
            username="dev_tasks",
            full_name="Developer One",
            password="Password123!",
        )
        self.company = Company.objects.create(name="Task Corp", code="TASK-CO")
        self.dept = Department.objects.create(company=self.company, name="R&D", code="RND")
        self.team = Team.objects.create(department=self.dept, name="Platform Team", code="PLAT")
        self.project = Project.objects.create(
            company=self.company,
            project_manager=self.pm,
            name="Core Platform",
            code="PRJ-CORE",
            status=Project.Status.ACTIVE,
        )

    def test_unified_task_and_subtask_hierarchy_and_progress_rollup(self):
        parent = Task.objects.create(
            project=self.project,
            title="Parent Feature Task",
            created_by=self.pm,
            task_owner=self.dev,
            progress_method=Task.ProgressMethod.SUBTASKS,
        )
        self.assertIsInstance(parent.id, uuid.UUID)
        self.assertFalse(parent.is_subtask)

        sub1 = Task.objects.create(
            project=self.project,
            parent_task=parent,
            title="Subtask 1",
            created_by=self.pm,
            status=Task.Status.COMPLETED,
        )
        sub2 = Task.objects.create(
            project=self.project,
            parent_task=parent,
            title="Subtask 2",
            created_by=self.pm,
            status=Task.Status.IN_PROGRESS,
            progress=Decimal("50.00"),
        )
        self.assertTrue(sub1.is_subtask)
        self.assertEqual(sub1.progress, Decimal("100.00"))

        parent.refresh_from_db()
        self.assertEqual(parent.progress, Decimal("75.00"))

    def test_checklist_auto_progress_calculation(self):
        task = Task.objects.create(
            project=self.project,
            title="Checklist Driven Task",
            created_by=self.pm,
            progress_method=Task.ProgressMethod.CHECKLIST,
        )
        checklist = Checklist.objects.create(
            task=task,
            title="Release Steps",
            created_by=self.pm,
        )
        item1 = ChecklistItem.objects.create(
            checklist=checklist,
            title="Step 1",
            is_completed=True,
            completed_by=self.dev,
        )
        ChecklistItem.objects.create(
            checklist=checklist,
            title="Step 2",
            is_completed=False,
        )
        task.refresh_from_db()
        self.assertEqual(task.progress, Decimal("50.00"))
        self.assertTrue(item1.is_completed)

    def test_task_assignment_user_and_team_and_reassignment_history(self):
        task = Task.objects.create(
            project=self.project,
            title="Assigned Task",
            created_by=self.pm,
        )
        user_assign = TaskAssignment.objects.create(
            task=task,
            assignment_type=TaskAssignment.AssignmentType.USER,
            user=self.dev,
            assigned_by=self.pm,
            status=TaskAssignment.Status.ACTIVE,
        )
        team_assign = TaskAssignment.objects.create(
            task=task,
            assignment_type=TaskAssignment.AssignmentType.TEAM,
            team=self.team,
            assigned_by=self.pm,
            status=TaskAssignment.Status.ACTIVE,
        )
        self.assertEqual(user_assign.user, self.dev)
        self.assertEqual(team_assign.team, self.team)

        # Target mismatch violates chk_task_assignments_target_match
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskAssignment.objects.create(
                    task=task,
                    assignment_type=TaskAssignment.AssignmentType.USER,
                    user=None,
                    team=self.team,
                    assigned_by=self.pm,
                )

        # Ending assignment preserves history and allows re-assigning the same user later
        user_assign.end_assignment(status=TaskAssignment.Status.REASSIGNED)
        self.assertIsNotNone(user_assign.ended_at)

        new_assign = TaskAssignment.objects.create(
            task=task,
            assignment_type=TaskAssignment.AssignmentType.USER,
            user=self.dev,
            assigned_by=self.pm,
            status=TaskAssignment.Status.ACTIVE,
        )
        self.assertEqual(new_assign.status, TaskAssignment.Status.ACTIVE)
        self.assertEqual(task.assignments.filter(user=self.dev).count(), 2)

    def test_task_dependencies_tags_and_comments(self):
        t1 = Task.objects.create(project=self.project, title="Task 1", created_by=self.pm)
        t2 = Task.objects.create(project=self.project, title="Task 2", created_by=self.pm)
        dep = TaskDependency.objects.create(task=t2, depends_on_task=t1)
        self.assertEqual(dep.dependency_type, TaskDependency.DependencyType.FINISH_TO_START)

        tag = TaskTag.objects.create(name="Backend")
        TaskTagAssignment.objects.create(task=t1, tag=tag)
        comment = TaskComment.objects.create(task=t1, author=self.dev, content="Ready for review")
        self.assertEqual(comment.author, self.dev)



from rest_framework.test import APITestCase
from apps.companies.models import CompanyMembership


class APIStructureAndSerializersTestCase(APITestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(
            email="alice@companya.com",
            username="alice_a",
            full_name="Alice A",
            password="Password123!",
        )
        self.user_b = User.objects.create_user(
            email="bob@companyb.com",
            username="bob_b",
            full_name="Bob B",
            password="Password123!",
        )
        self.company_a = Company.objects.create(name="Company A", code="COMP-A")
        self.company_b = Company.objects.create(name="Company B", code="COMP-B")
        CompanyMembership.objects.create(company=self.company_a, user=self.user_a, status="Active")
        CompanyMembership.objects.create(company=self.company_b, user=self.user_b, status="Active")

        self.dept_a = Department.objects.create(company=self.company_a, name="Eng A", code="ENGA")
        self.dept_b = Department.objects.create(company=self.company_b, name="Eng B", code="ENGB")

        self.project_a = Project.objects.create(
            company=self.company_a,
            project_manager=self.user_a,
            name="Project A",
            code="PRJ-A",
            status=Project.Status.ACTIVE,
        )

    def test_cross_company_validation_in_task_serializer(self):
        # Attempting to assign a department or owner from Company B to Project A must fail validation
        res = self.client.post(
            "/api/tasks/",
            {
                "project": str(self.project_a.id),
                "title": "Cross-company invalid task",
                "department": str(self.dept_b.id),
                "task_owner": str(self.user_b.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)

        # Valid same-company task creation succeeds
        res_ok = self.client.post(
            "/api/tasks/",
            {
                "project": str(self.project_a.id),
                "title": "Valid Company A Task",
                "department": str(self.dept_a.id),
                "task_owner": str(self.user_a.id),
                "priority": "High",
                "status": "To Do",
            },
            format="json",
        )
        self.assertEqual(res_ok.status_code, 201)

    def test_dependency_cycle_detection_and_paginated_history_endpoints(self):
        t1 = Task.objects.create(project=self.project_a, title="Task A1", created_by=self.user_a)
        t2 = Task.objects.create(project=self.project_a, title="Task A2", created_by=self.user_a)

        # T2 depends on T1 -> 201
        r1 = self.client.post(
            "/api/tasks/dependencies/",
            {"task": str(t2.id), "depends_on_task": str(t1.id)},
            format="json",
        )
        self.assertEqual(r1.status_code, 201)

        # T1 depends on T2 -> cycle detected -> 400
        r_cycle = self.client.post(
            "/api/tasks/dependencies/",
            {"task": str(t1.id), "depends_on_task": str(t2.id)},
            format="json",
        )
        self.assertEqual(r_cycle.status_code, 400)

        # Paginated history endpoints on TaskViewSet (DB Design §8)
        c_post = self.client.post(
            f"/api/tasks/{t1.id}/comments/",
            {"content": "First comment via task history endpoint"},
            format="json",
        )
        self.assertEqual(c_post.status_code, 201)

        p_post = self.client.post(
            f"/api/tasks/{t1.id}/progress-updates/",
            {"progress_percentage": "45.00", "work_completed": "Initial API wiring"},
            format="json",
        )
        self.assertEqual(p_post.status_code, 201)
        t1.refresh_from_db()
        self.assertEqual(t1.progress, Decimal("45.00"))

        rev_post = self.client.post(
            f"/api/tasks/{t1.id}/reviews/",
            {"decision": "Approved", "comments": "LGTM"},
            format="json",
        )
        self.assertEqual(rev_post.status_code, 201)
        t1.refresh_from_db()
        self.assertEqual(t1.status, Task.Status.APPROVED)

        dash_res = self.client.get(f"/api/dashboards/summary/?company={self.company_a.id}")
        self.assertEqual(dash_res.status_code, 200)
        self.assertEqual(dash_res.data["projects"]["total_projects"], 1)

    def test_task_crud_create_read_update_delete_api(self):
        """
        End-to-end test for SRS §7.1–§7.11 Task & Subtask Create, Read/Filter, Update/Edit, and Delete APIs.
        """
        self.client.force_authenticate(user=self.user_a)

        dev2 = User.objects.create_user(
            email="dev2@companya.com",
            username="dev2_a",
            full_name="Dev Two A",
            password="Password123!",
        )
        CompanyMembership.objects.create(company=self.company_a, user=dev2, status="Active")
        team_a = Team.objects.create(department=self.dept_a, name="API Team A", code="APIA")

        # 1. CREATE Task with full SRS §7.1 fields + assignees + tags + initial checklist
        create_res = self.client.post(
            "/api/tasks/",
            {
                "project": str(self.project_a.id),
                "department": str(self.dept_a.id),
                "team": str(team_a.id),
                "title": "Build OAuth2 Token Service",
                "description": "Implement JWT rotation and revocation endpoints.",
                "task_owner": str(self.user_a.id),
                "reviewer": str(dev2.id),
                "priority": "Critical",
                "status": "To Do",
                "start_date": "2026-11-01",
                "due_date": "2026-11-10",
                "estimated_effort": "24.50",
                "approval_required": True,
                "assignee_user_ids": [str(self.user_a.id)],
                "assignee_team_ids": [str(team_a.id)],
                "tag_names": ["security", "oauth2", "backend"],
                "initial_checklist_items": ["Define token model", "Add refresh endpoint"],
            },
            format="json",
        )
        self.assertEqual(create_res.status_code, 201, create_res.content)
        task_id = create_res.data["id"]
        self.assertEqual(create_res.data["title"], "Build OAuth2 Token Service")
        self.assertEqual(create_res.data["status"], "Assigned")  # auto-transitioned from To Do upon assignment
        self.assertEqual(len(create_res.data["assignee_user_ids"]), 1)
        self.assertEqual(len(create_res.data["assignee_team_ids"]), 1)
        self.assertEqual(set(create_res.data["tag_names"]), {"security", "oauth2", "backend"})

        # 2. CREATE Subtask under parent_task
        sub_res = self.client.post(
            "/api/tasks/",
            {
                "project": str(self.project_a.id),
                "parent_task": task_id,
                "title": "Write OAuth2 Unit Tests",
                "priority": "High",
                "assignee_user_ids": [str(dev2.id)],
            },
            format="json",
        )
        self.assertEqual(sub_res.status_code, 201, sub_res.content)
        subtask_id = sub_res.data["id"]
        self.assertTrue(sub_res.data["is_subtask"])
        self.assertEqual(sub_res.data["parent_task_title"], "Build OAuth2 Token Service")

        # 3. READ & FILTER Task List (SRS §7.11)
        list_res = self.client.get(
            f"/api/tasks/?company={self.company_a.id}&tag=oauth2&is_subtask=false"
        )
        self.assertEqual(list_res.status_code, 200)
        items = list_res.data["results"] if "results" in list_res.data else list_res.data
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], task_id)

        # 4. UPDATE / EDIT Task (PATCH /api/tasks/{id}/) including assignee reassignment & tags
        patch_res = self.client.patch(
            f"/api/tasks/{task_id}/",
            {
                "title": "Build OAuth2 Token Service v2",
                "priority": "High",
                "estimated_effort": "30.00",
                "assignee_user_ids": [str(dev2.id)],  # reassigns from user_a to dev2
                "tag_names": ["security", "v2"],
            },
            format="json",
        )
        self.assertEqual(patch_res.status_code, 200, patch_res.content)
        self.assertEqual(patch_res.data["title"], "Build OAuth2 Token Service v2")
        self.assertEqual(patch_res.data["assignee_user_ids"], [str(dev2.id)])
        self.assertEqual(set(patch_res.data["tag_names"]), {"security", "v2"})
        # Previous assignment for user_a is preserved as 'Reassigned' (DB Design §4.4 & §5)
        self.assertTrue(
            TaskAssignment.objects.filter(
                task_id=task_id, user=self.user_a, status=TaskAssignment.Status.REASSIGNED
            ).exists()
        )

        # 5. DELETE Task (DELETE /api/tasks/{id}/) cleanly removes task, subtasks & protected records
        del_res = self.client.delete(f"/api/tasks/{task_id}/")
        self.assertEqual(del_res.status_code, 204)
        self.assertFalse(Task.objects.filter(id=task_id).exists())
        self.assertFalse(Task.objects.filter(id=subtask_id).exists())

    def test_task_assignment_priority_and_due_dates_workflow(self):
        """
        Tests SRS §7.2 (Individual, Team, Reassignment, Bulk Assignment, Acknowledge),
        SRS §7.4 (Priority), SRS §7.10 (Due dates, Rescheduling, Overdue & Due Status).
        """
        from datetime import timedelta
        from django.utils import timezone
        from apps.notifications.models import Notification
        from apps.organization.models import TeamMembership

        self.client.force_authenticate(user=self.user_a)

        dev = User.objects.create_user(
            email="assign_dev@companya.com",
            username="assign_dev",
            full_name="Assigned Dev",
            password="Password123!",
        )
        lead = User.objects.create_user(
            email="assign_lead@companya.com",
            username="assign_lead",
            full_name="Team Lead A",
            password="Password123!",
        )
        CompanyMembership.objects.create(company=self.company_a, user=dev, status="Active")
        CompanyMembership.objects.create(company=self.company_a, user=lead, status="Active")

        team = Team.objects.create(department=self.dept_a, name="Platform Team", code="PLAT")
        TeamMembership.objects.create(team=team, user=lead, is_leader=True, status="Active")

        today = timezone.now().date()
        t1 = Task.objects.create(
            project=self.project_a,
            title="Database Indexing",
            created_by=self.user_a,
            priority=Task.Priority.MEDIUM,
            due_date=today - timedelta(days=2),
        )
        t2 = Task.objects.create(
            project=self.project_a,
            title="Cache Warming Job",
            created_by=self.user_a,
            priority=Task.Priority.LOW,
            due_date=today + timedelta(days=1),
        )

        # 1. Verify overdue & due_status calculation
        t1_res = self.client.get(f"/api/tasks/{t1.id}/")
        self.assertEqual(t1_res.status_code, 200)
        self.assertTrue(t1_res.data["is_overdue"])
        self.assertEqual(t1_res.data["due_status"], "Overdue")
        self.assertEqual(t1_res.data["days_remaining"], -2)

        t2_res = self.client.get(f"/api/tasks/{t2.id}/")
        self.assertEqual(t2_res.data["due_status"], "Due Soon")

        # 2. Team assignment coordinates Team Leader as default task_owner when entire_team_responsible=False
        team_assign_res = self.client.post(
            f"/api/tasks/{t1.id}/assign/",
            {
                "assignment_type": "Team",
                "team": str(team.id),
                "entire_team_responsible": False,
            },
            format="json",
        )
        self.assertEqual(team_assign_res.status_code, 201, team_assign_res.content)
        t1.refresh_from_db()
        self.assertEqual(t1.task_owner_id, lead.id)
        self.assertEqual(t1.status, Task.Status.ASSIGNED)

        # 3. Acknowledge assignment
        ack_res = self.client.post(
            f"/api/tasks/assignments/{team_assign_res.data['id']}/acknowledge/"
        )
        self.assertEqual(ack_res.status_code, 200)
        self.assertEqual(ack_res.data["status"], "Acknowledged")

        # 4. Bulk assign user + Critical priority + new due_date across both tasks
        new_due = today + timedelta(days=7)
        bulk_res = self.client.post(
            "/api/tasks/bulk_assign/",
            {
                "task_ids": [str(t1.id), str(t2.id)],
                "user": str(dev.id),
                "priority": "Critical",
                "due_date": str(new_due),
                "replace_existing": True,
            },
            format="json",
        )
        self.assertEqual(bulk_res.status_code, 200, bulk_res.content)
        self.assertEqual(bulk_res.data["updated_count"], 2)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.priority, "Critical")
        self.assertEqual(t2.priority, "Critical")
        self.assertEqual(t1.due_date, new_due)
        self.assertTrue(
            TaskAssignment.objects.filter(
                task=t1, team=team, status=TaskAssignment.Status.REASSIGNED
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(user=dev, related_task=t1).exists()
        )

        # 5. Reschedule task with invalid date (due_date < start_date) returns 400
        bad_sched = self.client.post(
            f"/api/tasks/{t1.id}/reschedule/",
            {
                "start_date": str(today + timedelta(days=10)),
                "due_date": str(today + timedelta(days=5)),
            },
            format="json",
        )
        self.assertEqual(bad_sched.status_code, 400)

        # Valid reschedule logs deadline change comment and updates priority & effort
        ok_sched = self.client.post(
            f"/api/tasks/{t1.id}/reschedule/",
            {
                "start_date": str(today),
                "due_date": str(today + timedelta(days=14)),
                "estimated_effort": "18.5",
                "priority": "High",
                "reason": "Client requested extended QA cycle",
            },
            format="json",
        )
        self.assertEqual(ok_sched.status_code, 200, ok_sched.content)
        self.assertEqual(ok_sched.data["priority"], "High")
        self.assertEqual(ok_sched.data["due_status"], "On Track")
        self.assertTrue(
            TaskComment.objects.filter(
                task=t1, content__icontains="Client requested extended QA cycle"
            ).exists()
        )

    def test_task_status_workflow_and_validation(self):
        """
        Tests SRS §7.3, §8.3, §12.2, §14.3, §20.2, §22 (#3, #5, #7, #9) & DB Design §4.7:
        1. Task without responsible owner/assignee cannot move to In Progress
        2. Invalid workflow transitions (e.g. Backlog -> Completed, To Do -> Approved) are rejected
        3. Approval-required task cannot bypass In Review / Approved to Completed
        4. Open subtask blocks Completed unless override_subtasks=True with override_reason
        5. PATCH /api/tasks/{id}/ enforces the same workflow transition rules
        6. Correction flow: In Review -> Changes Requested -> In Progress -> In Review -> Approved -> Completed
        7. DB-configured WorkflowTransition overrides default transitions when present
        """
        from apps.system_settings.models import WorkflowStatus, WorkflowTransition

        self.client.force_authenticate(user=self.user_a)

        # Task with no owner yet in Backlog
        task = Task.objects.create(
            project=self.project_a,
            title="Workflow Guarded Task",
            created_by=self.user_a,
            status=Task.Status.BACKLOG,
            approval_required=True,
        )

        # 1. Invalid jump Backlog -> Completed rejected
        r_bad_jump = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Completed"},
            format="json",
        )
        self.assertEqual(r_bad_jump.status_code, 400)

        # 2. Moving to In Progress without owner/assignee rejected (SRS §22 #3)
        r_no_owner = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "In Progress"},
            format="json",
        )
        self.assertEqual(r_no_owner.status_code, 400)

        # Assign owner & move Backlog -> To Do -> In Progress
        task.task_owner = self.user_a
        task.save(update_fields=["task_owner"])

        r_todo = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "To Do"},
            format="json",
        )
        self.assertEqual(r_todo.status_code, 200)

        # PATCH /api/tasks/{id}/ also rejects invalid transition To Do -> Approved
        r_patch_bad = self.client.patch(
            f"/api/tasks/{task.id}/",
            {"status": "Approved"},
            format="json",
        )
        self.assertEqual(r_patch_bad.status_code, 400)

        r_prog = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "In Progress", "reason": "Starting implementation"},
            format="json",
        )
        self.assertEqual(r_prog.status_code, 200)

        # 3. Approval-required task cannot skip In Review/Approved to Completed
        r_skip_review = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Completed"},
            format="json",
        )
        self.assertEqual(r_skip_review.status_code, 400)
        self.assertNotIn("Completed", r_prog.data["allowed_transitions"])

        # 4. Correction loop: In Progress -> In Review -> Changes Requested -> In Progress -> In Review -> Approved
        for st in ("In Review", "Changes Requested", "In Progress", "In Review", "Approved"):
            res = self.client.post(
                f"/api/tasks/{task.id}/transition-status/",
                {"status": st},
                format="json",
            )
            self.assertEqual(res.status_code, 200, res.content)

        # 5. Open subtask blocks Completed unless override_subtasks + override_reason provided
        sub = Task.objects.create(
            project=self.project_a,
            parent_task=task,
            title="Mandatory Subtask",
            created_by=self.user_a,
            status=Task.Status.IN_PROGRESS,
        )
        r_open_sub = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Completed"},
            format="json",
        )
        self.assertEqual(r_open_sub.status_code, 400)

        # Override without reason -> 400
        r_override_no_reason = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {"status": "Completed", "override_subtasks": True},
            format="json",
        )
        self.assertEqual(r_override_no_reason.status_code, 400)

        # Override with reason -> 200
        r_override_ok = self.client.post(
            f"/api/tasks/{task.id}/transition-status/",
            {
                "status": "Completed",
                "override_subtasks": True,
                "override_reason": "Subtask deferred to Phase 2",
            },
            format="json",
        )
        self.assertEqual(r_override_ok.status_code, 200, r_override_ok.content)
        self.assertEqual(r_override_ok.data["status"], "Completed")
        self.assertTrue(
            TaskComment.objects.filter(
                task=task, content__icontains="Subtask deferred to Phase 2"
            ).exists()
        )

        # 6. DB-configured WorkflowTransition overrides default transitions
        ws_todo = WorkflowStatus.objects.create(
            company=self.company_a,
            entity_type="Task",
            status_code="To Do",
            display_name="To Do",
            sort_order=1,
        )
        ws_hold = WorkflowStatus.objects.create(
            company=self.company_a,
            entity_type="Task",
            status_code="On Hold",
            display_name="On Hold",
            sort_order=2,
        )
        WorkflowTransition.objects.create(
            company=self.company_a,
            entity_type="Task",
            from_status=ws_todo,
            to_status=ws_hold,
        )
        t_cfg = Task.objects.create(
            project=self.project_a,
            title="Configured Workflow Task",
            created_by=self.user_a,
            task_owner=self.user_a,
            status=Task.Status.TO_DO,
        )
        allowed_res = self.client.get(f"/api/tasks/{t_cfg.id}/allowed-transitions/")
        self.assertEqual(allowed_res.status_code, 200)
        self.assertEqual(allowed_res.data["allowed_transitions"], ["On Hold"])
        sub.delete()



