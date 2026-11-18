from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.models import User
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.common.selectors import scope_queryset_by_company
from .filters import ProgressUpdateFilterSet, TimeEntryFilterSet
from .models import ProgressAttachment, ProgressUpdate, TimeEntry
from .serializers import (
    ProgressAttachmentSerializer,
    ProgressUpdateSerializer,
    TimeEntrySerializer,
)
from .services import get_work_log_review_summary_service


def _default_user(request):
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        return request.user
    return User.objects.first()


class ProgressUpdateViewSet(viewsets.ModelViewSet):
    serializer_class = ProgressUpdateSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ProgressUpdateFilterSet
    search_fields = [
        "work_completed",
        "remaining_work",
        "remarks",
        "blockers",
        "next_action",
        "task__title",
        "author__full_name",
    ]
    ordering_fields = ["created_at", "progress_percentage", "time_spent_minutes"]

    def get_queryset(self):
        qs = (
            ProgressUpdate.objects.select_related("task", "task__project", "author")
            .prefetch_related("progress_attachments__attachment__uploaded_by")
            .order_by("-created_at")
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "author" not in serializer.validated_data:
            task = serializer.validated_data.get("task")
            author = _default_user(self.request)
            if (
                not (self.request and getattr(self.request, "user", None) and self.request.user.is_authenticated)
                and task is not None
            ):
                author = task.task_owner or task.created_by or author
            serializer.save(author=author)
        else:
            serializer.save()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        SRS §9.3 Work Log Review:
        Returns manager work log review analytics comparing planned vs completed work,
        estimated vs actual time, reported progress vs approved deliverables, employee
        workload, and time spent on delayed tasks.
        """
        filtered_qs = self.filter_queryset(self.get_queryset())
        return Response(get_work_log_review_summary_service(filtered_qs))


class ProgressAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = ProgressAttachmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["progress_update", "attachment"]

    def get_queryset(self):
        qs = ProgressAttachment.objects.select_related(
            "progress_update", "progress_update__task__project", "attachment", "attachment__uploaded_by"
        ).all()
        return scope_queryset_by_company(
            qs, self.request, company_lookup="progress_update__task__project__company_id"
        )


class TimeEntryViewSet(viewsets.ModelViewSet):
    serializer_class = TimeEntrySerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = TimeEntryFilterSet
    ordering_fields = ["started_at", "created_at", "duration_minutes"]

    def get_queryset(self):
        qs = TimeEntry.objects.select_related("task", "task__project", "user", "approved_by").order_by("-created_at")
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "user" not in serializer.validated_data:
            serializer.save(user=_default_user(self.request))
        else:
            serializer.save()
