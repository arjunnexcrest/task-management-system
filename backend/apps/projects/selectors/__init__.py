from django.db.models import Count, Q
from apps.common.permissions import (
    get_user_company_ids,
    is_ceo,
    is_user_active,
)
from apps.common.selectors import scope_queryset_by_company
from apps.companies.models import CompanyMembership
from apps.projects.models import Project, ProjectMember
from apps.rbac.roles import SystemRole


def get_authorized_project_ids(user, company_ids=None) -> set | None:
    """
    Returns the set of Project UUIDs the active user is authorized to access
    (SRS §2.1–§2.8, §4.3 & PERM-04), or None for CEO (unrestricted across companies).
    - CEO: unrestricted (None).
    - CompanyInCharge: all projects in companies where they hold CompanyInCharge.
    - Other roles (PM, DepartmentHead, TeamLeader, Employee): projects within their
      active companies where they are:
        * the accountable project_manager, OR
        * an Active ProjectMember, OR
        * an Active Department Head of a participating ProjectDepartment, OR
        * an Active Team Leader of a participating ProjectTeam, OR
        * owner, reviewer, or active assignee (individual or team) of a task in the project.
    """
    if not is_user_active(user):
        return set()
    if is_ceo(user):
        return None

    active_cids = get_user_company_ids(user)
    if company_ids is not None:
        active_cids = active_cids & set(company_ids)
    if not active_cids:
        return set()

    from apps.organization.models import DepartmentMembership, TeamMembership

    incharge_cids = set(
        CompanyMembership.objects.filter(
            user=user,
            company_id__in=active_cids,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
            role__iexact=SystemRole.COMPANY_IN_CHARGE,
        ).values_list("company_id", flat=True)
    )
    try:
        from apps.rbac.models import MembershipRole

        incharge_cids.update(
            MembershipRole.objects.filter(
                membership__user=user,
                membership__company_id__in=active_cids,
                membership__is_active=True,
                membership__status=CompanyMembership.Status.ACTIVE,
                role__code__iexact=SystemRole.COMPANY_IN_CHARGE,
            ).values_list("membership__company_id", flat=True)
        )
    except Exception:
        pass

    headed_dept_ids = set(
        DepartmentMembership.objects.filter(
            user=user,
            department__company_id__in=active_cids,
            status=DepartmentMembership.Status.ACTIVE,
            is_head=True,
        ).values_list("department_id", flat=True)
    )
    active_team_ids = set(
        TeamMembership.objects.filter(
            user=user,
            team__department__company_id__in=active_cids,
            status=TeamMembership.Status.ACTIVE,
        ).values_list("team_id", flat=True)
    )
    led_team_ids = set(
        TeamMembership.objects.filter(
            user=user,
            team__department__company_id__in=active_cids,
            status=TeamMembership.Status.ACTIVE,
            is_leader=True,
        ).values_list("team_id", flat=True)
    )

    project_filter = (
        Q(project_manager=user)
        | Q(members__user=user, members__status=ProjectMember.Status.ACTIVE)
        | Q(tasks__task_owner=user)
        | Q(tasks__reviewer=user)
        | Q(tasks__assignments__user=user, tasks__assignments__status__in=["Active", "Acknowledged"])
    )
    if incharge_cids:
        project_filter |= Q(company_id__in=incharge_cids)
    if headed_dept_ids:
        project_filter |= Q(project_departments__department_id__in=headed_dept_ids)
    if led_team_ids:
        project_filter |= Q(project_teams__team_id__in=led_team_ids)
    if active_team_ids:
        project_filter |= Q(
            tasks__assignments__team_id__in=active_team_ids,
            tasks__assignments__status__in=["Active", "Acknowledged"],
        )

    return set(
        Project.objects.filter(company_id__in=active_cids)
        .filter(project_filter)
        .values_list("id", flat=True)
        .distinct()
    )


def scope_queryset_by_project(
    queryset,
    request,
    project_lookup="project_id",
    company_lookup="project__company_id",
):
    """
    Scopes any project-linked queryset to both the user's authorized companies
    and their authorized projects (SRS §4.3 & DB Design §7 Rule #5).
    """
    if request is None:
        return queryset

    qs = scope_queryset_by_company(queryset, request, company_lookup=company_lookup)
    user = getattr(request, "user", None)
    if not is_user_active(user):
        return qs.none()
    if is_ceo(user):
        return qs

    authorized_project_ids = get_authorized_project_ids(user)
    if not authorized_project_ids:
        return qs.none()
    return qs.filter(**{f"{project_lookup}__in": authorized_project_ids})


def get_project_list_queryset(request=None):
    """
    Company- and project-authorization-scoped project list queryset using
    select_related, prefetch_related, and database aggregation (DB Design §6 & §7 Rules #5, #8).
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
    )
    return scope_queryset_by_project(qs, request, project_lookup="id", company_lookup="company_id")


def get_project_detail_queryset(request=None):
    """
    Company- and project-authorization-scoped project detail queryset.
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

