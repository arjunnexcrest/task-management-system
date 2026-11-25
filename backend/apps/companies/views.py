from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.response import Response

from apps.common.permissions import (
    CompanyManagementPermission,
    UserManagementPermission,
)
from apps.common.selectors import scope_queryset_by_company
from .models import Company, CompanyMembership
from .serializers import CompanyMembershipSerializer, CompanySerializer


class CompanyViewSet(viewsets.ModelViewSet):
    serializer_class = CompanySerializer
    permission_classes = [CompanyManagementPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "code"]
    search_fields = ["name", "code", "address"]
    ordering_fields = ["name", "code", "created_at"]

    def get_queryset(self):
        qs = Company.objects.prefetch_related("memberships").all()
        qs = scope_queryset_by_company(qs, self.request, company_lookup="id")
        is_active_param = self.request.query_params.get("is_active")
        if is_active_param is not None and is_active_param != "":
            flag = str(is_active_param).lower() in ("true", "1", "yes")
            if flag:
                qs = qs.filter(status=Company.Status.ACTIVE)
            else:
                qs = qs.exclude(status=Company.Status.ACTIVE)
        return qs

    def destroy(self, request, *args, **kwargs):
        """Soft-deactivate company to preserve historical projects, tasks, and memberships (DB Design §5)."""
        company = self.get_object()
        company.status = Company.Status.INACTIVE
        company.save(update_fields=["status", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class CompanyMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyMembershipSerializer
    permission_classes = [UserManagementPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["company", "user", "status"]

    def get_queryset(self):
        qs = CompanyMembership.objects.select_related("company", "user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="company_id")
