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

