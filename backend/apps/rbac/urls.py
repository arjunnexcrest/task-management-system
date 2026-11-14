from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    MembershipRoleViewSet,
    PermissionViewSet,
    RolePermissionViewSet,
    RoleViewSet,
)

router = DefaultRouter()
router.register(r"roles", RoleViewSet, basename="role")
router.register(r"permissions", PermissionViewSet, basename="permission")
router.register(r"membership-roles", MembershipRoleViewSet, basename="membership-role")
router.register(r"role-permissions", RolePermissionViewSet, basename="role-permission")

urlpatterns = [
    path("", include(router.urls)),
]
