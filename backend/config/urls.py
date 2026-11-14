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
    from apps.companies.models import Company
    from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership

    companies = list(Company.objects.values("id", "name", "code", "status"))

    head_map = {}
    for dm in DepartmentMembership.objects.filter(
        is_head=True, status=DepartmentMembership.Status.ACTIVE
    ).select_related("user"):
        head_map[dm.department_id] = {
            "id": str(dm.user_id),
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
    for tm in TeamMembership.objects.filter(
        is_leader=True, status=TeamMembership.Status.ACTIVE
    ).select_related("user"):
        leader_map[tm.team_id] = {
            "id": str(tm.user_id),
            "full_name": tm.user.full_name,
        }

    teams = []
    for t in Team.objects.select_related("department").all():
        leader = leader_map.get(t.id)
        teams.append(
            {
                "id": str(t.id),
                "department_id": str(t.department_id),
                "department_name": t.department.name,
                "company_id": str(t.department.company_id),
                "name": t.name,
                "code": t.code,
                "status": t.status,
                "team_leader_id": leader["id"] if leader else None,
                "team_leader_name": leader["full_name"] if leader else None,
            }
        )

    users = list(
        User.objects.values(
            "id",
            "email",
            "username",
            "full_name",
            "employee_id",
            "designation",
            "account_status",
        )
    )

    return JsonResponse(
        {
            "companies": companies,
            "departments": departments,
            "teams": teams,
            "users": users,
        }
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
