from django.db.models import Count, Q
from apps.common.selectors import scope_queryset_by_company
from apps.projects.models import Project


def get_project_list_queryset(request=None):
    """
    Company-scoped project list queryset using select_related, prefetch_related, and database aggregation
    to avoid N+1 queries (DB Design §6 & §7 Rules #5, #8).
    """
    qs = (
        Project.objects.select_related("company", "project_manager")
        .prefetch_related(
            "members__user",
            "project_departments__department",
            "project_teams__team",
        )
        .annotate(
            top_tasks_count=Count("tasks", filter=Q(tasks__parent_task__isnull=True), distinct=True),
            done_tasks_count=Count(
                "tasks",
                filter=Q(tasks__parent_task__isnull=True, tasks__status="Completed"),
                distinct=True,
            ),
        )
        .order_by("-created_at")
    )
    return scope_queryset_by_company(qs, request, company_lookup="company_id")


def get_project_detail_queryset(request=None):
    """
    Company-scoped project detail queryset with select_related and prefetch_related.
    """
    qs = get_project_list_queryset(request).prefetch_related(
        "members__user",
        "project_departments__department",
        "project_teams__team",
        "phases__owner",
        "milestones__owner",
        "milestones__milestone_tasks__task",
        "milestones__dependencies__depends_on_milestone",
    )
    return qs

