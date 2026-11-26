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


def lookup_data(request):
    from apps.accounts.models import User
    from apps.companies.models import Company, CompanyMembership
    from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership

    companies = list(Company.objects.values("id", "name", "code", "status"))

    head_map = {}
    user_dept_ids: dict[str, list[str]] = {}
    user_dept_names: dict[str, list[str]] = {}
    for dm in DepartmentMembership.objects.filter(
        status=DepartmentMembership.Status.ACTIVE
    ).select_related("user", "department"):
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
    for d in Department.objects.all():
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
    for tm in TeamMembership.objects.filter(
        status=TeamMembership.Status.ACTIVE
    ).select_related("user", "team"):
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
    for t in Team.objects.select_related("department").all():
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
    for cm in CompanyMembership.objects.filter(
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
    ):
        user_company_ids.setdefault(str(cm.user_id), []).append(str(cm.company_id))

    users = []
    for u in User.objects.all():
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

    return JsonResponse(
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
    # Auth, Users, Employees, Memberships, Departments, Teams & Reporting APIs
    path("api/auth/", include("apps.accounts.urls")),
    path("api/users/", include(user_only_router.urls)),
    path("api/employees/", include(user_only_router.urls)),
    path("api/memberships/", include(membership_router.urls)),
    path("api/departments/", include(department_only_urlpatterns)),
    path("api/teams/", include(team_only_urlpatterns)),
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
