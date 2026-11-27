from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    DepartmentMembershipViewSet,
    DepartmentViewSet,
    OrganizationHierarchyEmployeeDetailView,
    OrganizationHierarchyView,
    ReportingRelationshipViewSet,
    TeamMembershipViewSet,
    TeamViewSet,
)

router = DefaultRouter()
router.register(r"departments", DepartmentViewSet, basename="department")
router.register(r"department-memberships", DepartmentMembershipViewSet, basename="department-membership")
router.register(r"teams", TeamViewSet, basename="team")
router.register(r"team-memberships", TeamMembershipViewSet, basename="team-membership")
router.register(r"reporting-relationships", ReportingRelationshipViewSet, basename="reporting-relationship")

# Standalone router for mounting directly at /api/departments/
department_only_router = DefaultRouter()
department_only_router.register(r"", DepartmentViewSet, basename="departments-api")

department_only_urlpatterns = [
    path(
        "<uuid:pk>/assign-head/",
        DepartmentViewSet.as_view({"post": "assign_head"}),
        name="departments-api-assign-head-hyphen",
    ),
    path("", include(department_only_router.urls)),
]

# Standalone router for mounting directly at /api/teams/
team_only_router = DefaultRouter()
team_only_router.register(r"", TeamViewSet, basename="teams-api")

team_only_urlpatterns = [
    path(
        "<uuid:pk>/assign-leader/",
        TeamViewSet.as_view({"post": "assign_leader"}),
        name="teams-api-assign-leader-hyphen",
    ),
    path("", include(team_only_router.urls)),
]

# Standalone router for mounting directly at /api/reporting-relationships/
reporting_only_router = DefaultRouter()
reporting_only_router.register(
    r"", ReportingRelationshipViewSet, basename="reporting-relationships-api"
)

reporting_only_urlpatterns = [
    path("", include(reporting_only_router.urls)),
]

# Standalone routes for mounting directly at /api/hierarchy/
hierarchy_only_urlpatterns = [
    path("", OrganizationHierarchyView.as_view(), name="hierarchy-api"),
    path(
        "employee/<uuid:user_id>/",
        OrganizationHierarchyEmployeeDetailView.as_view(),
        name="hierarchy-api-employee-detail",
    ),
]

urlpatterns = [
    path("hierarchy/", OrganizationHierarchyView.as_view(), name="organization-hierarchy"),
    path(
        "hierarchy/employee/<uuid:user_id>/",
        OrganizationHierarchyEmployeeDetailView.as_view(),
        name="organization-hierarchy-employee-detail",
    ),
    path(
        "departments/<uuid:pk>/assign-head/",
        DepartmentViewSet.as_view({"post": "assign_head"}),
        name="department-assign-head-hyphen",
    ),
    path(
        "teams/<uuid:pk>/assign-leader/",
        TeamViewSet.as_view({"post": "assign_leader"}),
        name="team-assign-leader-hyphen",
    ),
    path("", include(router.urls)),
]
