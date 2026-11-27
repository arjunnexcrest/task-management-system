from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsActiveUser, IsCEO, IsCompanyMember
from apps.common.selectors import scope_queryset_by_company
from .models import MembershipRole, Permission, Role, RolePermission
from .serializers import (
    MembershipRoleSerializer,
    PermissionSerializer,
    RolePermissionSerializer,
    RoleSerializer,
)


class RoleViewSet(viewsets.ModelViewSet):
    """
    Role catalogue API at /api/rbac/roles/ (SRS §2.1 & §2.8):
    - Read: authenticated company members.
    - Create / Edit / Delete: CEO only. System roles cannot be deleted.
    """

    queryset = Role.objects.prefetch_related("role_permissions__permission").all().order_by("name")
    serializer_class = RoleSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_system_role", "code"]
    search_fields = ["name", "code", "description"]
    ordering_fields = ["name", "code"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), IsCEO()]

    def perform_destroy(self, instance: Role):
        if instance.is_system_role:
            raise ValidationError({"detail": "System roles cannot be deleted."})
        super().perform_destroy(instance)


class PermissionViewSet(viewsets.ModelViewSet):
    """Permission catalogue API at /api/rbac/permissions/ — Read: company members; Write: CEO."""

    queryset = Permission.objects.all().order_by("code")
    serializer_class = PermissionSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["code", "description"]
    ordering_fields = ["code"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), IsCEO()]


class MembershipRoleViewSet(viewsets.ModelViewSet):
    """Company-scoped MembershipRole assignments at /api/rbac/membership-roles/."""

    serializer_class = MembershipRoleSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["membership", "role"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), IsCEO()]

    def get_queryset(self):
        qs = (
            MembershipRole.objects.select_related(
                "membership", "membership__user", "membership__company", "role", "assigned_by"
            )
            .all()
            .order_by("-assigned_at")
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="membership__company_id")

    def perform_create(self, serializer):
        serializer.save(assigned_by=self.request.user)


class RolePermissionViewSet(viewsets.ModelViewSet):
    """Role-to-Permission mappings at /api/rbac/role-permissions/."""

    queryset = RolePermission.objects.select_related("role", "permission").all().order_by("role__code", "permission__code")
    serializer_class = RolePermissionSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["role", "permission"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), IsCEO()]
