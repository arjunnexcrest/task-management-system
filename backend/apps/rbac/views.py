from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.common.selectors import scope_queryset_by_company
from .models import MembershipRole, Permission, Role, RolePermission
from .serializers import (
    MembershipRoleSerializer,
    PermissionSerializer,
    RolePermissionSerializer,
    RoleSerializer,
)


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.prefetch_related("role_permissions__permission").all()
    serializer_class = RoleSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["is_system_role", "code"]


class PermissionViewSet(viewsets.ModelViewSet):
    queryset = Permission.objects.all()
    serializer_class = PermissionSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]


class MembershipRoleViewSet(viewsets.ModelViewSet):
    serializer_class = MembershipRoleSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["membership", "role"]

    def get_queryset(self):
        qs = MembershipRole.objects.select_related(
            "membership", "membership__user", "membership__company", "role", "assigned_by"
        ).all()
        return scope_queryset_by_company(qs, self.request, company_lookup="membership__company_id")


class RolePermissionViewSet(viewsets.ModelViewSet):
    queryset = RolePermission.objects.select_related("role", "permission").all()
    serializer_class = RolePermissionSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["role", "permission"]
