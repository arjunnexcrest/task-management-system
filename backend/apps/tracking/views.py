from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from apps.accounts.models import User
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.common.selectors import scope_queryset_by_company
from .models import ProgressAttachment, ProgressUpdate, TimeEntry
from .serializers import (
    ProgressAttachmentSerializer,
    ProgressUpdateSerializer,
    TimeEntrySerializer,
)


def _default_user(request):
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        return request.user
    return User.objects.first()


class ProgressUpdateViewSet(viewsets.ModelViewSet):
    serializer_class = ProgressUpdateSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "author"]
    ordering_fields = ["created_at", "progress_percentage"]

    def get_queryset(self):
        qs = ProgressUpdate.objects.select_related("task", "task__project", "author").order_by("-created_at")
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "author" not in serializer.validated_data:
            serializer.save(author=_default_user(self.request))
        else:
            serializer.save()


class ProgressAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = ProgressAttachmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["progress_update", "attachment"]

    def get_queryset(self):
        qs = ProgressAttachment.objects.select_related(
            "progress_update", "progress_update__task__project", "attachment"
        ).all()
        return scope_queryset_by_company(
            qs, self.request, company_lookup="progress_update__task__project__company_id"
        )


class TimeEntryViewSet(viewsets.ModelViewSet):
    serializer_class = TimeEntrySerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "user", "entry_type", "approval_status"]
    ordering_fields = ["started_at", "created_at", "duration_minutes"]

    def get_queryset(self):
        qs = TimeEntry.objects.select_related("task", "task__project", "user", "approved_by").order_by("-created_at")
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "user" not in serializer.validated_data:
            serializer.save(user=_default_user(self.request))
        else:
            serializer.save()
