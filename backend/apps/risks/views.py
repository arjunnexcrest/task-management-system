from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from apps.accounts.models import User
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev, RiskManagementPermission
from apps.common.selectors import scope_queryset_by_company
from .models import Blocker, Issue, Risk
from .serializers import BlockerSerializer, IssueSerializer, RiskSerializer


def _default_user(request):
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        return request.user
    return User.objects.first()


class BlockerViewSet(viewsets.ModelViewSet):
    serializer_class = BlockerSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "resolution_status", "impact", "responsible_person"]
    ordering_fields = ["reported_at"]

    def get_queryset(self):
        qs = Blocker.objects.select_related(
            "task", "task__project", "responsible_person", "reported_by"
        ).order_by("-reported_at")
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "reported_by" not in serializer.validated_data:
            serializer.save(reported_by=_default_user(self.request))
        else:
            serializer.save()


class RiskViewSet(viewsets.ModelViewSet):
    serializer_class = RiskSerializer
    permission_classes = [RiskManagementPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["project", "status", "severity", "probability", "impact", "owner"]
    search_fields = ["title", "description"]
    ordering_fields = ["due_date", "title"]

    def get_queryset(self):
        qs = Risk.objects.select_related("project", "owner").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="project__company_id")


class IssueViewSet(viewsets.ModelViewSet):
    serializer_class = IssueSerializer
    permission_classes = [RiskManagementPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["project", "task", "status", "priority", "owner"]
    search_fields = ["title", "description"]
    ordering_fields = ["reported_at", "due_date"]

    def get_queryset(self):
        qs = Issue.objects.select_related("project", "task", "owner", "reported_by").order_by("-reported_at")
        return scope_queryset_by_company(qs, self.request, company_lookup="project__company_id")

    def perform_create(self, serializer):
        if "reported_by" not in serializer.validated_data:
            serializer.save(reported_by=_default_user(self.request))
        else:
            serializer.save()
