import math
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.models import User
from apps.common.permissions import (
    IsCompanyMemberOrAllowAnyInDev,
    ROLE_DELIVERY_LEADS,
    user_has_any_role,
)
from apps.common.selectors import scope_queryset_by_company
from apps.tasks.models import Task
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

    @action(detail=False, methods=["get"], url_path="active-timer")
    def active_timer(self, request):
        """Returns the caller's currently running timer entry (if any)."""
        actor = _default_user(request)
        if not actor:
            return Response({"active_timer": None})
        timer = (
            self.get_queryset()
            .filter(user=actor, entry_type=TimeEntry.EntryType.TIMER, ended_at__isnull=True)
            .first()
        )
        return Response(
            {"active_timer": TimeEntrySerializer(timer).data if timer else None}
        )

    @action(detail=False, methods=["post"], url_path="start-timer")
    def start_timer(self, request):
        """
        SRS §9.2 & DB Design §4.6:
        Starts a live timer on a task. Enforces one active timer per employee (`uq_active_timer_per_user`).
        """
        actor = _default_user(request)
        task_id = request.data.get("task")
        if not actor or not task_id:
            return Response(
                {"error": "A valid `task` UUID is required to start a timer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = TimeEntry.objects.filter(
            user=actor,
            entry_type=TimeEntry.EntryType.TIMER,
            ended_at__isnull=True,
        ).select_related("task").first()
        if existing:
            return Response(
                {
                    "error": f"You already have an active timer running on '{existing.task.title}'. Stop it before starting another timer (SRS §9.2).",
                    "active_timer": TimeEntrySerializer(existing).data,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        task = scope_queryset_by_company(
            Task.objects.select_related("project"),
            request,
            company_lookup="project__company_id",
        ).filter(id=task_id).first()
        if not task:
            return Response({"error": "Task not found in your authorized company scope."}, status=status.HTTP_404_NOT_FOUND)

        entry = TimeEntry.objects.create(
            task=task,
            user=actor,
            entry_type=TimeEntry.EntryType.TIMER,
            started_at=timezone.now(),
            ended_at=None,
            duration_minutes=0,
        )
        return Response(TimeEntrySerializer(entry).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="stop-timer")
    def stop_timer(self, request):
        """Stops the caller's active timer and records elapsed duration_minutes (minimum 1m)."""
        actor = _default_user(request)
        if not actor:
            return Response({"error": "User not resolved."}, status=status.HTTP_400_BAD_REQUEST)

        timer = TimeEntry.objects.filter(
            user=actor,
            entry_type=TimeEntry.EntryType.TIMER,
            ended_at__isnull=True,
        ).select_related("task", "task__project", "user").first()
        if not timer:
            return Response(
                {"error": "No active timer is currently running."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        elapsed_secs = max(60.0, (now - (timer.started_at or now)).total_seconds())
        timer.ended_at = now
        timer.duration_minutes = max(1, int(math.ceil(elapsed_secs / 60.0)))
        timer.save(update_fields=["ended_at", "duration_minutes"])
        return Response(TimeEntrySerializer(timer).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Allows a manager/lead to approve or reject a time entry."""
        if request.user and request.user.is_authenticated:
            if not user_has_any_role(request.user, ROLE_DELIVERY_LEADS):
                return Response(
                    {"detail": "Only Team Leaders, Department Heads, Project Managers, or Executives can approve time entries."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        entry = self.get_object()
        decision = request.data.get("approval_status", TimeEntry.ApprovalStatus.APPROVED)
        if decision not in (
            TimeEntry.ApprovalStatus.APPROVED,
            TimeEntry.ApprovalStatus.REJECTED,
            TimeEntry.ApprovalStatus.PENDING,
        ):
            return Response({"error": "Invalid approval_status."}, status=status.HTTP_400_BAD_REQUEST)
        entry.approval_status = decision
        entry.approved_by = _default_user(request)
        entry.save(update_fields=["approval_status", "approved_by"])
        return Response(TimeEntrySerializer(entry).data, status=status.HTTP_200_OK)
