from datetime import timedelta
from django.db.models import Avg, Count, Exists, OuterRef, Q, Sum
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.projects.selectors import get_project_list_queryset
from apps.tasks.models import Task, TaskDependency
from apps.tasks.selectors import get_task_list_queryset
from apps.tasks.services.dependencies import STARTED_STATUSES, TERMINAL_STATUSES


class DashboardSummaryView(APIView):
    """
    Implements DB Design §6 & §8:
    'Dashboard: aggregate counts and progress at company/project/department/team/employee
    levels using filtered querysets and database aggregation. Ensure dashboard filters use
    the same authorization rules as task lists.'
    """

    permission_classes = [IsCompanyMemberOrAllowAnyInDev]

    def get(self, request):
        projects_qs = get_project_list_queryset(request)
        tasks_qs = get_task_list_queryset(request)

        project_id = request.query_params.get("project")
        department_id = request.query_params.get("department")
        team_id = request.query_params.get("team")
        employee_id = request.query_params.get("employee")

        if project_id:
            projects_qs = projects_qs.filter(id=project_id)
            tasks_qs = tasks_qs.filter(project_id=project_id)
        if department_id:
            tasks_qs = tasks_qs.filter(department_id=department_id)
        if team_id:
            tasks_qs = tasks_qs.filter(team_id=team_id)
        if employee_id:
            tasks_qs = tasks_qs.filter(
                Q(task_owner_id=employee_id)
                | Q(assignments__user_id=employee_id, assignments__status__in=["Active", "Acknowledged"])
            ).distinct()

        today = timezone.now().date()

        project_stats = projects_qs.aggregate(
            total_projects=Count("id", distinct=True),
            active_projects=Count("id", filter=Q(status="Active"), distinct=True),
            completed_projects=Count("id", filter=Q(status="Completed"), distinct=True),
            at_risk_projects=Count("id", filter=Q(status__in=["At Risk", "Delayed"]), distinct=True),
            average_project_progress=Avg("progress"),
            total_budget=Sum("budget"),
        )

        started_list = list(STARTED_STATUSES)
        terminal_list = list(TERMINAL_STATUSES)
        unsat_start_subq = TaskDependency.objects.filter(task_id=OuterRef("pk")).filter(
            (
                Q(dependency_type=TaskDependency.DependencyType.FINISH_TO_START)
                & ~Q(depends_on_task__status=Task.Status.COMPLETED)
            )
            | (
                Q(dependency_type=TaskDependency.DependencyType.START_TO_START)
                & ~Q(depends_on_task__status__in=started_list)
            )
        )
        unsat_complete_subq = TaskDependency.objects.filter(task_id=OuterRef("pk")).filter(
            (
                Q(
                    dependency_type__in=[
                        TaskDependency.DependencyType.FINISH_TO_START,
                        TaskDependency.DependencyType.FINISH_TO_FINISH,
                    ]
                )
                & ~Q(depends_on_task__status=Task.Status.COMPLETED)
            )
            | (
                Q(
                    dependency_type__in=[
                        TaskDependency.DependencyType.START_TO_START,
                        TaskDependency.DependencyType.START_TO_FINISH,
                    ]
                )
                & ~Q(depends_on_task__status__in=started_list)
            )
        )
        annotated_tasks_qs = tasks_qs.annotate(
            _unsat_start=Exists(unsat_start_subq),
            _unsat_complete=Exists(unsat_complete_subq),
        )
        dep_blocked_q = (
            (~Q(status__in=started_list) & ~Q(status__in=terminal_list) & Q(_unsat_start=True))
            | (Q(status__in=started_list) & ~Q(status__in=terminal_list) & Q(_unsat_complete=True))
        )

        task_stats = annotated_tasks_qs.aggregate(
            total_tasks_and_subtasks=Count("id", distinct=True),
            top_level_tasks=Count("id", filter=Q(parent_task__isnull=True), distinct=True),
            subtasks=Count("id", filter=Q(parent_task__isnull=False), distinct=True),
            completed_tasks=Count("id", filter=Q(status="Completed"), distinct=True),
            in_progress_tasks=Count("id", filter=Q(status="In Progress"), distinct=True),
            blocked_tasks=Count("id", filter=Q(status="Blocked"), distinct=True),
            dependency_blocked_tasks=Count("id", filter=dep_blocked_q, distinct=True),
            overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            critical_overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today)
                & ~Q(status__in=terminal_list)
                & (Q(priority="Critical") | Q(due_date__lt=today - timedelta(days=7))),
                distinct=True,
            ),
            due_today_tasks=Count(
                "id",
                filter=Q(due_date=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            due_soon_tasks=Count(
                "id",
                filter=Q(due_date__gt=today, due_date__lte=today + timedelta(days=3))
                & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            average_task_progress=Avg("progress"),
            total_estimated_effort=Sum("estimated_effort"),
        )

        by_status = list(
            tasks_qs.values("status").annotate(count=Count("id", distinct=True)).order_by("status")
        )
        by_priority = list(
            tasks_qs.values("priority").annotate(count=Count("id", distinct=True)).order_by("priority")
        )

        return Response(
            {
                "projects": project_stats,
                "tasks": task_stats,
                "tasks_by_status": by_status,
                "tasks_by_priority": by_priority,
            }
        )
