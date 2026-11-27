from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)


def api_root(request):
    return JsonResponse(
        {
            "service": "Task Management System (TMS) API",
            "status": "running",
            "database": "PostgreSQL (45 UUID Tables per Official Database Design)",
            "endpoints": {
                "admin": "/admin/",
                "swagger_docs": "/api/docs/",
                "redoc": "/api/redoc/",
                "lookup": "/api/lookup/",
                "auth_login": "/api/accounts/login/",
                "auth_me": "/api/accounts/me/",
                "auth_logout": "/api/accounts/logout/",
                "auth_refresh": "/api/auth/refresh/",
                "accounts": "/api/accounts/users/",
                "companies": "/api/companies/",
                "company_memberships": "/api/companies/memberships/",
                "organization_departments": "/api/organization/departments/",
                "organization_teams": "/api/organization/teams/",
                "organization_reporting": "/api/organization/reporting-relationships/",
                "rbac_roles": "/api/rbac/roles/",
                "rbac_permissions": "/api/rbac/permissions/",
                "rbac_membership_roles": "/api/rbac/membership-roles/",
                "projects": "/api/projects/",
                "project_members": "/api/projects/members/",
                "project_departments": "/api/projects/departments/",
                "project_teams": "/api/projects/teams/",
                "project_phases": "/api/projects/phases/",
                "project_milestones": "/api/projects/milestones/",
                "milestone_tasks": "/api/projects/milestone-tasks/",
                "milestone_dependencies": "/api/projects/milestone-dependencies/",
                "tasks": "/api/tasks/",
                "task_assignments": "/api/tasks/assignments/",
                "task_dependencies": "/api/tasks/dependencies/",
                "task_tags": "/api/tasks/tags/",
                "task_tag_assignments": "/api/tasks/tag-assignments/",
                "checklists": "/api/tasks/checklists/",
                "checklist_items": "/api/tasks/checklist-items/",
                "comments": "/api/tasks/comments/",
                "attachments": "/api/tasks/attachments/",
                "tracking_progress_updates": "/api/tracking/progress-updates/",
                "tracking_time_entries": "/api/tracking/time-entries/",
                "reviews": "/api/reviews/",
                "notifications": "/api/notifications/",
                "dashboards_summary": "/api/dashboards/summary/",
                "dashboards_ceo": "/api/dashboards/ceo/",
                "dashboards_role": "/api/dashboards/role/",
                "reports_tasks": "/api/reports/tasks/",
                "risks_blockers": "/api/risks/blockers/",
                "risks_register": "/api/risks/risks/",
                "risks_issues": "/api/risks/issues/",
                "settings": "/api/settings/",
                "workflow_statuses": "/api/settings/workflow-statuses/",
                "workflow_transitions": "/api/settings/workflow-transitions/",
            },
        }
    )


from rest_framework import status as drf_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response as DRFResponse
from apps.common.permissions import IsActiveUser


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsActiveUser])
def lookup_data(request):
    from apps.accounts.models import User
    from apps.common.permissions import (
        get_company_id_from_request,
        get_user_company_ids,
        is_ceo,
    )
    from apps.companies.models import Company, CompanyMembership
    from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership

    user = request.user
    ceo = is_ceo(user)
    requested_company_id = get_company_id_from_request(request)

    if ceo:
        allowed_company_ids = None
    else:
        allowed_company_ids = get_user_company_ids(user)
        if not allowed_company_ids:
            return DRFResponse(
                {"detail": "You are not an active member of any company."},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if requested_company_id:
            allowed_strs = {str(cid) for cid in allowed_company_ids}
            if str(requested_company_id) not in allowed_strs:
                return DRFResponse(
                    {"detail": "You are not an active member of this company."},
                    status=drf_status.HTTP_403_FORBIDDEN,
                )

    companies_qs = Company.objects.all()
    if allowed_company_ids is not None:
        companies_qs = companies_qs.filter(id__in=allowed_company_ids, is_active=True)
    if requested_company_id:
        companies_qs = companies_qs.filter(id=requested_company_id)
    companies = list(companies_qs.values("id", "name", "code", "status"))
    scoped_company_ids = {c["id"] for c in companies}

    head_map = {}
    user_dept_ids: dict[str, list[str]] = {}
    user_dept_names: dict[str, list[str]] = {}
    dm_qs = DepartmentMembership.objects.filter(
        status=DepartmentMembership.Status.ACTIVE,
        department__company_id__in=scoped_company_ids,
    ).select_related("user", "department")
    for dm in dm_qs:
        uid_s = str(dm.user_id)
        did_s = str(dm.department_id)
        user_dept_ids.setdefault(uid_s, []).append(did_s)
        user_dept_names.setdefault(uid_s, []).append(dm.department.name)
        if dm.is_head:
            head_map[dm.department_id] = {
                "id": uid_s,
                "full_name": dm.user.full_name,
            }

    departments = []
    for d in Department.objects.filter(company_id__in=scoped_company_ids):
        head = head_map.get(d.id)
        departments.append(
            {
                "id": str(d.id),
                "company_id": str(d.company_id),
                "name": d.name,
                "code": d.code,
                "status": d.status,
                "department_head_id": head["id"] if head else None,
                "department_head_name": head["full_name"] if head else None,
            }
        )

    leader_map = {}
    team_member_ids_map: dict[str, list[str]] = {}
    user_team_ids: dict[str, list[str]] = {}
    user_team_names: dict[str, list[str]] = {}
    tm_qs = TeamMembership.objects.filter(
        status=TeamMembership.Status.ACTIVE,
        team__department__company_id__in=scoped_company_ids,
    ).select_related("user", "team")
    for tm in tm_qs:
        uid_s = str(tm.user_id)
        tid_s = str(tm.team_id)
        team_member_ids_map.setdefault(tid_s, []).append(uid_s)
        user_team_ids.setdefault(uid_s, []).append(tid_s)
        user_team_names.setdefault(uid_s, []).append(tm.team.name)
        if tm.is_leader:
            leader_map[tm.team_id] = {
                "id": uid_s,
                "full_name": tm.user.full_name,
            }

    teams = []
    for t in Team.objects.filter(
        department__company_id__in=scoped_company_ids
    ).select_related("department"):
        leader = leader_map.get(t.id)
        tid_s = str(t.id)
        m_ids = team_member_ids_map.get(tid_s, [])
        teams.append(
            {
                "id": tid_s,
                "department_id": str(t.department_id),
                "department_name": t.department.name,
                "company_id": str(t.department.company_id),
                "name": t.name,
                "code": t.code,
                "status": t.status,
                "team_leader_id": leader["id"] if leader else None,
                "team_leader_name": leader["full_name"] if leader else None,
                "active_members_count": len(m_ids),
                "member_ids": m_ids,
            }
        )

    user_company_ids: dict[str, list[str]] = {}
    scoped_user_ids = set()
    for cm in CompanyMembership.objects.filter(
        company_id__in=scoped_company_ids,
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
    ):
        user_company_ids.setdefault(str(cm.user_id), []).append(str(cm.company_id))
        scoped_user_ids.add(cm.user_id)

    users_qs = User.objects.all() if (ceo and not requested_company_id) else User.objects.filter(id__in=scoped_user_ids)
    users = []
    for u in users_qs:
        uid_s = str(u.id)
        users.append(
            {
                "id": uid_s,
                "email": u.email,
                "username": u.username,
                "full_name": u.full_name,
                "employee_id": u.employee_id,
                "designation": u.designation,
                "account_status": u.account_status,
                "company_ids": user_company_ids.get(uid_s, []),
                "department_ids": user_dept_ids.get(uid_s, []),
                "department_names": user_dept_names.get(uid_s, []),
                "team_ids": user_team_ids.get(uid_s, []),
                "team_names": user_team_names.get(uid_s, []),
            }
        )

    return DRFResponse(
        {
            "companies": companies,
            "departments": departments,
            "teams": teams,
            "users": users,
        }
    )


from apps.accounts.urls import user_only_router
from apps.companies.urls import membership_router
from apps.organization.urls import (
    department_only_urlpatterns,
    hierarchy_only_urlpatterns,
    reporting_only_urlpatterns,
    team_only_urlpatterns,
)

urlpatterns = [
    path("", api_root, name="api-root"),
    path("admin/", admin.site.urls),
    path("api/lookup/", lookup_data, name="api-lookup"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
    # Auth, Users, Employees, Memberships, Departments, Teams, Hierarchy & Reporting APIs
    path("api/auth/", include("apps.accounts.urls")),
    path("api/users/", include(user_only_router.urls)),
    path("api/employees/", include(user_only_router.urls)),
    path("api/memberships/", include(membership_router.urls)),
    path("api/departments/", include(department_only_urlpatterns)),
    path("api/teams/", include(team_only_urlpatterns)),
    path("api/hierarchy/", include(hierarchy_only_urlpatterns)),
    path("api/reporting-relationships/", include(reporting_only_urlpatterns)),
    # All 13 SRS & Database Design Module APIs Wired
    path("api/accounts/", include("apps.accounts.urls")),
    path("api/companies/", include("apps.companies.urls")),
    path("api/organization/", include("apps.organization.urls")),
    path("api/rbac/", include("apps.rbac.urls")),
    path("api/projects/", include("apps.projects.urls")),
    path("api/tasks/", include("apps.tasks.urls")),
    path("api/tracking/", include("apps.tracking.urls")),
    path("api/reviews/", include("apps.reviews.urls")),
    path("api/notifications/", include("apps.notifications.urls")),
    path("api/dashboards/", include("apps.dashboards.urls")),
    path("api/reports/", include("apps.reports.urls")),
    path("api/risks/", include("apps.risks.urls")),
    path("api/settings/", include("apps.system_settings.urls")),
]
