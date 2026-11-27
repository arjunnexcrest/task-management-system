from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from apps.common.permissions import IsActiveUser, IsCompanyMember, is_ceo
from apps.tasks.permissions import user_is_task_manager
from apps.tasks.selectors import get_authorized_task_ids, scope_queryset_by_task
from .models import ProgressAttachment, ProgressUpdate, TimeEntry
from .serializers import (
    ProgressAttachmentSerializer,
    ProgressUpdateSerializer,
    TimeEntrySerializer,
)


def _ensure_task_authorized(user, task):
    if is_ceo(user):
        return
    allowed = get_authorized_task_ids(user) or set()
    if task.id not in allowed:
        raise PermissionDenied("You do not have access to this task.")


class ProgressUpdateViewSet(viewsets.ModelViewSet):
    serializer_class = ProgressUpdateSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "author"]
    ordering_fields = ["created_at", "progress_percentage"]

    def get_queryset(self):
        qs = ProgressUpdate.objects.select_related("task", "task__project", "author").order_by("-created_at")
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


class ProgressAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = ProgressAttachmentSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["progress_update", "attachment"]

    def get_queryset(self):
        qs = ProgressAttachment.objects.select_related(
            "progress_update", "progress_update__task", "progress_update__task__project", "attachment"
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
    filterset_fields = ["task", "user", "entry_type", "approval_status"]
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
