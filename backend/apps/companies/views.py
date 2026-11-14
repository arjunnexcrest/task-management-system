from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.common.selectors import scope_queryset_by_company
from .models import Company, CompanyMembership
from .serializers import CompanyMembershipSerializer, CompanySerializer


class CompanyViewSet(viewsets.ModelViewSet):
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "code"]
    search_fields = ["name", "code", "address"]
    ordering_fields = ["name", "code", "created_at"]


class CompanyMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyMembershipSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["company", "user", "status"]

    def get_queryset(self):
        qs = CompanyMembership.objects.select_related("company", "user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="company_id")
