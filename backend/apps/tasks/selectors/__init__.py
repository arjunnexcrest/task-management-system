from django.db.models import Count, Q
from apps.common.permissions import get_user_company_ids, is_ceo, is_user_active
from apps.common.selectors import scope_queryset_by_company
from apps.projects.selectors import get_authorized_project_ids
from apps.tasks.models import Task


def get_authorized_task_ids(user) -> set | None:
    """
    Returns the set of Task UUIDs the active user is authorized to access,
    or None for CEO (unrestricted).
    """
    if not is_user_active(user):
        return set()
    if is_ceo(user):
        return None

    active_cids = get_user_company_ids(user)
    if not active_cids:
        return set()

    from apps.organization.models import TeamMembership

    authorized_project_ids = get_authorized_project_ids(user, company_ids=active_cids) or set()
    active_team_ids = set(
        TeamMembership.objects.filter(
            user=user,
            team__department__company_id__in=active_cids,
            status=TeamMembership.Status.ACTIVE,
        ).values_list("team_id", flat=True)
    )

    task_filter = (
        Q(task_owner=user)
        | Q(reviewer=user)
        | Q(reviews__reviewer=user)
        | Q(assignments__user=user, assignments__status__in=["Active", "Acknowledged"])
    )
    if authorized_project_ids:
        task_filter |= Q(project_id__in=authorized_project_ids)
    if active_team_ids:
        task_filter |= Q(
            assignments__team_id__in=active_team_ids,
            assignments__status__in=["Active", "Acknowledged"],
        )

    return set(
        Task.objects.filter(project__company_id__in=active_cids)
        .filter(task_filter)
        .values_list("id", flat=True)
        .distinct()
    )


def scope_queryset_by_task(
    queryset,
    request,
    task_lookup="task_id",
    company_lookup="task__project__company_id",
):
    """
    Scopes any task-linked queryset to the user's authorized companies & tasks.
    """
    if request is None:
        return queryset

    qs = scope_queryset_by_company(queryset, request, company_lookup=company_lookup)
    user = getattr(request, "user", None)
    if not is_user_active(user):
        return qs.none()
    if is_ceo(user):
        return qs

    authorized_task_ids = get_authorized_task_ids(user)
    if not authorized_task_ids:
        return qs.none()
    return qs.filter(**{f"{task_lookup}__in": authorized_task_ids})


def get_task_list_queryset(request=None):
    """
    Implements DB Design §6 & §8 + SRS §4.3:
    - Scoped by company (`project__company_id`) AND authorized projects/tasks.
    - Uses select_related for single-valued FKs and prefetch_related for M2M/reverse relations.
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
            "dependencies__depends_on_task__task_owner",
            "dependent_tasks__task__task_owner",
            "time_entries",
        )
        .annotate(annotated_subtasks_count=Count("subtasks", distinct=True))
        .order_by("-created_at")
    )
    return scope_queryset_by_task(
        qs, request, task_lookup="id", company_lookup="project__company_id"
    )


def get_task_detail_queryset(request=None):
    """
    Implements DB Design §8 Task Detail retrieval pattern:
    'Task detail: retrieve task + project + owner + creator + department/team with select_related;
    retrieve assignments, checklists and tags with prefetch_related.'
    """
    return get_task_list_queryset(request).prefetch_related(
        "subtasks__task_owner",
        "dependencies__depends_on_task__task_owner",
        "dependent_tasks__task__task_owner",
        "checklists__items__completed_by",
        "comments__author",
        "comments__mentions__mentioned_user",
        "comments__comment_attachments__attachment__uploaded_by",
        "progress_updates__author",
        "progress_updates__progress_attachments__attachment__uploaded_by",
        "task_attachments__attachment__uploaded_by",
        "reviews__reviewer",
    )
