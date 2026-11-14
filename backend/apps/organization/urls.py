from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    DepartmentMembershipViewSet,
    DepartmentViewSet,
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

urlpatterns = [
    path("", include(router.urls)),
]
