import math
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsActiveUser, IsCompanyMember, is_ceo
from apps.tasks.models import Task
from apps.tasks.permissions import user_is_task_manager
from apps.tasks.selectors import get_authorized_task_ids, scope_queryset_by_task
from .filters import ProgressUpdateFilterSet, TimeEntryFilterSet
from .models import ProgressAttachment, ProgressUpdate, TimeEntry
from .serializers import (
    ProgressAttachmentSerializer,
    ProgressUpdateSerializer,
    TimeEntrySerializer,
)
from .services import get_work_log_review_summary_service


def _ensure_task_authorized(user, task):
    if is_ceo(user):
        return
    allowed = get_authorized_task_ids(user) or set()
    if task.id not in allowed:
        raise PermissionDenied("You do not have access to this task.")


class ProgressUpdateViewSet(viewsets.ModelViewSet):
    serializer_class = ProgressUpdateSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
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
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        task = serializer.validated_data["task"]
        _ensure_task_authorized(self.request.user, task)
        is_mgr = user_is_task_manager(self.request.user, task)
        supplied_author = serializer.validated_data.get("author")
        if not is_mgr and supplied_author is not None and supplied_author.pk != self.request.user.pk:
            raise PermissionDenied("You cannot log progress updates on behalf of another user.")
        author = supplied_author if (is_mgr and supplied_author is not None) else self.request.user
        serializer.save(author=author)

    def perform_update(self, serializer):
        instance = serializer.instance
        is_mgr = user_is_task_manager(self.request.user, instance.task)
        if instance.author_id != self.request.user.id and not is_mgr:
            raise PermissionDenied("You can only edit your own progress updates.")
        if not is_mgr:
            supplied_author = serializer.validated_data.get("author")
            if supplied_author is not None and supplied_author.pk != self.request.user.pk:
                raise PermissionDenied("You cannot reassign progress update authorship.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.author_id != self.request.user.id and not user_is_task_manager(
            self.request.user, instance.task
        ):
            raise PermissionDenied("You can only delete your own progress updates.")
        instance.delete()

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
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["progress_update", "attachment"]

    def get_queryset(self):
        qs = ProgressAttachment.objects.select_related(
            "progress_update",
            "progress_update__task",
            "progress_update__task__project",
            "attachment",
            "attachment__uploaded_by",
        ).all()
        return scope_queryset_by_task(
            qs,
            self.request,
            task_lookup="progress_update__task_id",
            company_lookup="progress_update__task__project__company_id",
        )

    def perform_create(self, serializer):
        progress_update = serializer.validated_data["progress_update"]
        _ensure_task_authorized(self.request.user, progress_update.task)
        if progress_update.author_id != self.request.user.id and not user_is_task_manager(
            self.request.user, progress_update.task
        ):
            raise PermissionDenied("You can only attach files to your own progress updates.")
        serializer.save()

    def perform_destroy(self, instance):
        pu = instance.progress_update
        if pu.author_id != self.request.user.id and not user_is_task_manager(self.request.user, pu.task):
            raise PermissionDenied("You can only remove attachments from your own progress updates.")
        instance.delete()


class TimeEntryViewSet(viewsets.ModelViewSet):
    serializer_class = TimeEntrySerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = TimeEntryFilterSet
    ordering_fields = ["started_at", "created_at", "duration_minutes"]

    def get_queryset(self):
        qs = TimeEntry.objects.select_related("task", "task__project", "user", "approved_by").order_by("-created_at")
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        task = serializer.validated_data["task"]
        _ensure_task_authorized(self.request.user, task)
        is_mgr = user_is_task_manager(self.request.user, task)

        supplied_user = serializer.validated_data.get("user")
        approval_status = serializer.validated_data.get("approval_status", TimeEntry.ApprovalStatus.PENDING)
        approved_by = serializer.validated_data.get("approved_by")

        if not is_mgr:
            if supplied_user is not None and supplied_user.pk != self.request.user.pk:
                raise PermissionDenied("You can only log time entries for yourself.")
            if approval_status != TimeEntry.ApprovalStatus.PENDING or approved_by is not None:
                raise PermissionDenied("Only managers can approve or reject time entries.")
            serializer.save(
                user=self.request.user,
                approval_status=TimeEntry.ApprovalStatus.PENDING,
                approved_by=None,
            )
            return

        target_user = supplied_user or self.request.user
        if (
            approval_status in (TimeEntry.ApprovalStatus.APPROVED, TimeEntry.ApprovalStatus.REJECTED)
            and approved_by is None
        ):
            approved_by = self.request.user
        serializer.save(user=target_user, approved_by=approved_by)

    def perform_update(self, serializer):
        instance = serializer.instance
        is_mgr = user_is_task_manager(self.request.user, instance.task)
        if instance.user_id != self.request.user.id and not is_mgr:
            raise PermissionDenied("You can only edit your own time entries.")

        if not is_mgr:
            supplied_user = serializer.validated_data.get("user")
            if supplied_user is not None and supplied_user.pk != self.request.user.pk:
                raise PermissionDenied("You cannot reassign a time entry to another user.")
            new_status = serializer.validated_data.get("approval_status")
            if (
                (new_status is not None and new_status != instance.approval_status)
                or "approved_by" in serializer.validated_data
            ):
                raise PermissionDenied("Only managers can approve or reject time entries.")
            serializer.save()
            return

        new_status = serializer.validated_data.get("approval_status")
        if (
            new_status in (TimeEntry.ApprovalStatus.APPROVED, TimeEntry.ApprovalStatus.REJECTED)
            and "approved_by" not in serializer.validated_data
        ):
            serializer.save(approved_by=self.request.user)
        else:
            serializer.save()

    def perform_destroy(self, instance):
        if instance.user_id != self.request.user.id and not user_is_task_manager(
            self.request.user, instance.task
        ):
            raise PermissionDenied("You can only delete your own time entries.")
        instance.delete()

    @action(detail=False, methods=["get"], url_path="active-timer")
    def active_timer(self, request):
        """Returns the caller's currently running timer entry (if any)."""
        actor = request.user
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
        actor = request.user
        task_id = request.data.get("task")
        if not task_id:
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

        task = scope_queryset_by_task(
            Task.objects.select_related("project"),
            request,
            task_lookup="id",
            company_lookup="project__company_id",
        ).filter(id=task_id).first()
        if not task:
            return Response({"error": "Task not found in your authorized scope."}, status=status.HTTP_404_NOT_FOUND)

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
        actor = request.user
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
        entry = self.get_object()
        if not user_is_task_manager(request.user, entry.task):
            return Response(
                {"detail": "Only Team Leaders, Department Heads, Project Managers, or Executives can approve time entries."},
                status=status.HTTP_403_FORBIDDEN,
            )
        decision = request.data.get("approval_status", TimeEntry.ApprovalStatus.APPROVED)
        if decision not in (
            TimeEntry.ApprovalStatus.APPROVED,
            TimeEntry.ApprovalStatus.REJECTED,
            TimeEntry.ApprovalStatus.PENDING,
        ):
            return Response({"error": "Invalid approval_status."}, status=status.HTTP_400_BAD_REQUEST)
        entry.approval_status = decision
        entry.approved_by = request.user
        entry.save(update_fields=["approval_status", "approved_by"])
        return Response(TimeEntrySerializer(entry).data, status=status.HTTP_200_OK)
