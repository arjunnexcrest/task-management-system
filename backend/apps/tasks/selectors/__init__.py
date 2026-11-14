from django.db.models import Count
from apps.common.selectors import scope_queryset_by_company
from apps.tasks.models import Task


def get_task_list_queryset(request=None):
    """
    Implements DB Design §6 & §8:
    - Scoped by company (`project__company_id`).
    - Uses select_related for single-valued FKs (project, parent_task, phase, department,
      team, task_owner, created_by, reviewer) and prefetch_related for assignments and tags.
    """
    qs = (
        Task.objects.select_related(
            "project",
            "project__company",
            "parent_task",
            "phase",
            "department",
            "team",
            "task_owner",
            "created_by",
            "reviewer",
        )
        .prefetch_related(
            "assignments__user",
            "assignments__team",
            "assignments__assigned_by",
            "tag_assignments__tag",
        )
        .annotate(annotated_subtasks_count=Count("subtasks", distinct=True))
    )
    return scope_queryset_by_company(qs, request, company_lookup="project__company_id")


def get_task_detail_queryset(request=None):
    """
    Implements DB Design §8 Task Detail retrieval pattern:
    'Task detail: retrieve task + project + owner + creator + department/team with select_related;
    retrieve assignments, checklists and tags with prefetch_related.'
    """
    return get_task_list_queryset(request).prefetch_related(
        "subtasks__task_owner",
        "dependencies__depends_on_task",
        "checklists__items__completed_by",
        "comments__author",
        "comments__mentions__mentioned_user",
        "task_attachments__attachment__uploaded_by",
    )
