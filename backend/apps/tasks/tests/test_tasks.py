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
