from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.projects.selectors import get_project_list_queryset
from apps.tasks.selectors import get_task_list_queryset


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

        task_stats = tasks_qs.aggregate(
            total_tasks_and_subtasks=Count("id", distinct=True),
            top_level_tasks=Count("id", filter=Q(parent_task__isnull=True), distinct=True),
            subtasks=Count("id", filter=Q(parent_task__isnull=False), distinct=True),
            completed_tasks=Count("id", filter=Q(status="Completed"), distinct=True),
            in_progress_tasks=Count("id", filter=Q(status="In Progress"), distinct=True),
            blocked_tasks=Count("id", filter=Q(status="Blocked"), distinct=True),
            overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today) & ~Q(status__in=["Completed", "Cancelled"]),
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
