from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.accounts.models import User
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.common.selectors import scope_queryset_by_company
from .filters import TaskAssignmentFilterSet, TaskFilterSet
from .models import (
    Attachment,
    Checklist,
    ChecklistItem,
    CommentAttachment,
    CommentMention,
    Task,
    TaskAssignment,
    TaskAttachment,
    TaskComment,
    TaskDependency,
    TaskTag,
    TaskTagAssignment,
)
from .selectors import get_task_detail_queryset, get_task_list_queryset
from .serializers import (
    AttachmentSerializer,
    ChecklistItemSerializer,
    ChecklistSerializer,
    CommentAttachmentSerializer,
    CommentMentionSerializer,
    SubtaskNestedSerializer,
    TaskAssignmentSerializer,
    TaskAttachmentSerializer,
    TaskCommentSerializer,
    TaskDependencySerializer,
    TaskDetailSerializer,
    TaskSerializer,
    TaskTagAssignmentSerializer,
    TaskTagSerializer,
)
from .services import (
    assign_task_service,
    bulk_assign_tasks_service,
    delete_task_service,
    get_allowed_next_statuses,
    reschedule_task_service,
    transition_task_status_service,
)


def _default_user(request):
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        return request.user
    return User.objects.first()


class TaskViewSet(viewsets.ModelViewSet):
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TaskFilterSet
    search_fields = ["title", "description", "project__name", "project__code"]
    ordering_fields = ["due_date", "start_date", "priority", "progress", "created_at", "updated_at", "title"]

    def get_queryset(self):
        if self.action == "retrieve":
            return get_task_detail_queryset(self.request)
        return get_task_list_queryset(self.request)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return TaskDetailSerializer
        return TaskSerializer

    def perform_create(self, serializer):
        if "created_by" not in serializer.validated_data:
            serializer.save(created_by=_default_user(self.request))
        else:
            serializer.save()

    def destroy(self, request, *args, **kwargs):
        task = self.get_object()
        try:
            delete_task_service(task=task)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        """
        SRS §7.2 & DB Design §4.4, §5, §7:
        Assigns a user or team to the task while preserving assignment history,
        supporting optional task_owner, reviewer, and entire_team_responsible flags.
        """
        task = self.get_object()
        try:
            assignment = assign_task_service(
                task=task,
                assignment_type=request.data.get("assignment_type", TaskAssignment.AssignmentType.USER),
                user_id=request.data.get("user"),
                team_id=request.data.get("team"),
                assigned_by_id=request.data.get("assigned_by") or getattr(_default_user(request), "id", None) or task.created_by_id,
                replace_existing=bool(request.data.get("replace_existing", False)),
                task_owner_id=request.data.get("task_owner"),
                reviewer_id=request.data.get("reviewer"),
                entire_team_responsible=bool(request.data.get("entire_team_responsible", False)),
            )
        except DjangoValidationError as exc:
            msg = exc.message_dict if hasattr(exc, "message_dict") else (exc.messages if hasattr(exc, "messages") else str(exc))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TaskAssignmentSerializer(assignment).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def bulk_assign(self, request):
        """
        SRS §7.2 Bulk Assignment, §7.4 Priority & §7.10 Due Dates:
        Atomically assigns users/teams, owner, reviewer, priority, or due dates across multiple tasks.
        """
        try:
            updated = bulk_assign_tasks_service(
                task_ids=request.data.get("task_ids", []),
                actor=_default_user(request),
                user_id=request.data.get("user"),
                assignee_user_ids=request.data.get("assignee_user_ids"),
                team_id=request.data.get("team"),
                assignee_team_ids=request.data.get("assignee_team_ids"),
                task_owner_id=request.data.get("task_owner"),
                reviewer_id=request.data.get("reviewer"),
                priority=request.data.get("priority"),
                due_date=request.data.get("due_date"),
                start_date=request.data.get("start_date"),
                replace_existing=bool(request.data.get("replace_existing", False)),
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "updated_count": len(updated),
                "tasks": TaskSerializer(updated, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        """
        SRS §7.4, §7.10 & §22 #12:
        Updates start_date, due_date, estimated_effort, and/or priority with deadline-change tracking.
        """
        task = self.get_object()
        try:
            updated = reschedule_task_service(
                task=task,
                start_date=request.data.get("start_date"),
                due_date=request.data.get("due_date"),
                estimated_effort=request.data.get("estimated_effort"),
                priority=request.data.get("priority"),
                reason=request.data.get("reason"),
                actor=_default_user(request),
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_task_detail_queryset(request).get(pk=updated.pk)
        return Response(TaskDetailSerializer(refreshed).data)

    @action(detail=True, methods=["get"], url_path="allowed-transitions")
    def allowed_transitions(self, request, pk=None):
        """
        SRS §7.3 & §20.2:
        Returns the permitted next workflow statuses for this task.
        """
        task = self.get_object()
        return Response(
            {
                "task_id": str(task.id),
                "current_status": task.status,
                "allowed_transitions": get_allowed_next_statuses(
                    task=task, actor=_default_user(request)
                ),
            }
        )

    @action(detail=True, methods=["post"], url_path="transition-status")
    def transition_status(self, request, pk=None):
        """
        SRS §7.3, §8.3, §14.3 & §22:
        Atomically transitions task status while validating permitted workflow paths,
        responsible owner, required approvals, and subtask completion / authorized overrides.
        """
        task = self.get_object()
        new_status = request.data.get("status")
        try:
            updated = transition_task_status_service(
                task=task,
                new_status=new_status,
                actor=_default_user(request),
                reason=request.data.get("reason"),
                override_subtasks=bool(request.data.get("override_subtasks", False)),
                override_reason=request.data.get("override_reason"),
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_task_detail_queryset(request).get(pk=updated.pk)
        return Response(TaskDetailSerializer(refreshed).data)

    @action(detail=True, methods=["get"])
    def subtasks(self, request, pk=None):
        """
        Paginated subtasks belonging to this parent task (`parent_task_id`).
        """
        task = self.get_object()
        qs = task.subtasks.select_related("task_owner").order_by("created_at")
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(SubtaskNestedSerializer(page, many=True).data)
        return Response(SubtaskNestedSerializer(qs, many=True).data)

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        """
        Paginated task comment history endpoint per DB Design §8:
        'Task history: expose paginated endpoints for comments, progress updates, reviews and time entries.'
        """
        task = self.get_object()
        if request.method == "POST":
            data = request.data.copy()
            data["task"] = str(task.id)
            serializer = TaskCommentSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            if "author" not in serializer.validated_data:
                serializer.save(author=_default_user(request))
            else:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        qs = task.comments.select_related("author").prefetch_related("mentions__mentioned_user").order_by("-created_at")
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(TaskCommentSerializer(page, many=True).data)
        return Response(TaskCommentSerializer(qs, many=True).data)

    @action(detail=True, methods=["get", "post"], url_path="progress-updates")
    def progress_updates(self, request, pk=None):
        """
        Paginated task progress updates history endpoint per DB Design §8.
        """
        from apps.tracking.serializers import ProgressUpdateSerializer

        task = self.get_object()
        if request.method == "POST":
            data = request.data.copy()
            data["task"] = str(task.id)
            serializer = ProgressUpdateSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            if "author" not in serializer.validated_data:
                serializer.save(author=_default_user(request))
            else:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        qs = task.progress_updates.select_related("author").order_by("-created_at")
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(ProgressUpdateSerializer(page, many=True).data)
        return Response(ProgressUpdateSerializer(qs, many=True).data)

    @action(detail=True, methods=["get", "post"])
    def reviews(self, request, pk=None):
        """
        Paginated task review history endpoint per DB Design §8.
        """
        from apps.reviews.serializers import TaskReviewSerializer

        task = self.get_object()
        if request.method == "POST":
            data = request.data.copy()
            data["task"] = str(task.id)
            serializer = TaskReviewSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            if "reviewer" not in serializer.validated_data:
                serializer.save(reviewer=_default_user(request))
            else:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        qs = task.reviews.select_related("reviewer").order_by("-reviewed_at")
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(TaskReviewSerializer(page, many=True).data)
        return Response(TaskReviewSerializer(qs, many=True).data)

    @action(detail=True, methods=["get", "post"], url_path="time-entries")
    def time_entries(self, request, pk=None):
        """
        Paginated task time entries history endpoint per DB Design §8.
        """
        from apps.tracking.serializers import TimeEntrySerializer

        task = self.get_object()
        if request.method == "POST":
            data = request.data.copy()
            data["task"] = str(task.id)
            serializer = TimeEntrySerializer(data=data)
            serializer.is_valid(raise_exception=True)
            if "user" not in serializer.validated_data:
                serializer.save(user=_default_user(request))
            else:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        qs = task.time_entries.select_related("user", "approved_by").order_by("-created_at")
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(TimeEntrySerializer(page, many=True).data)
        return Response(TimeEntrySerializer(qs, many=True).data)

    @action(detail=False, methods=["get"])
    def kanban(self, request):
        tasks = self.filter_queryset(self.get_queryset().filter(parent_task__isnull=True))
        columns = {s[0]: [] for s in Task.Status.choices}
        for item in TaskSerializer(tasks, many=True).data:
            if item["status"] in columns:
                columns[item["status"]].append(item)
        return Response(columns)


class TaskAssignmentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskAssignmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = TaskAssignmentFilterSet
    ordering_fields = ["assigned_at", "ended_at"]

    def get_queryset(self):
        qs = TaskAssignment.objects.select_related("task", "task__project", "user", "team", "assigned_by").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "assigned_by" not in serializer.validated_data:
            serializer.save(assigned_by=_default_user(self.request))
        else:
            serializer.save()

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        assignment = self.get_object()
        assignment.status = TaskAssignment.Status.ACKNOWLEDGED
        assignment.save(update_fields=["status"])
        return Response(TaskAssignmentSerializer(assignment).data)

    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        assignment = self.get_object()
        new_status = request.data.get("status", TaskAssignment.Status.REASSIGNED)
        assignment.end_assignment(status=new_status)
        return Response(TaskAssignmentSerializer(assignment).data)


class TaskDependencyViewSet(viewsets.ModelViewSet):
    serializer_class = TaskDependencySerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task", "depends_on_task", "dependency_type"]

    def get_queryset(self):
        qs = TaskDependency.objects.select_related("task", "task__project", "depends_on_task").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")


class TaskTagViewSet(viewsets.ModelViewSet):
    queryset = TaskTag.objects.all()
    serializer_class = TaskTagSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name"]


class TaskTagAssignmentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskTagAssignmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task", "tag"]

    def get_queryset(self):
        qs = TaskTagAssignment.objects.select_related("task", "task__project", "tag").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")


class ChecklistViewSet(viewsets.ModelViewSet):
    serializer_class = ChecklistSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task"]

    def get_queryset(self):
        qs = Checklist.objects.select_related("task", "task__project").prefetch_related("items").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "created_by" not in serializer.validated_data:
            serializer.save(created_by=_default_user(self.request))
        else:
            serializer.save()

    @action(detail=True, methods=["post"])
    def reorder_items(self, request, pk=None):
        """
        SRS §8.4: Reorders checklist items according to the supplied item_ids sequence.
        """
        checklist = self.get_object()
        item_ids = request.data.get("item_ids", [])
        if not isinstance(item_ids, list) or not item_ids:
            return Response(
                {"error": "Provide a non-empty list of item_ids to reorder."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            items_by_id = {str(it.id): it for it in checklist.items.all()}
            order = 1
            for raw_id in item_ids:
                it = items_by_id.get(str(raw_id))
                if it:
                    ChecklistItem.objects.filter(id=it.id).update(sort_order=order)
                    order += 1
        checklist.refresh_from_db()
        return Response(ChecklistSerializer(checklist).data)


class ChecklistItemViewSet(viewsets.ModelViewSet):
    serializer_class = ChecklistItemSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["checklist", "is_completed"]

    def get_queryset(self):
        qs = ChecklistItem.objects.select_related("checklist", "checklist__task__project", "completed_by").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="checklist__task__project__company_id")

    def perform_destroy(self, instance):
        task = instance.checklist.task
        instance.delete()
        task.recalculate_auto_progress()

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        item = self.get_object()
        item.is_completed = not item.is_completed
        item.completed_at = timezone.now() if item.is_completed else None
        if item.is_completed and not item.completed_by_id:
            item.completed_by = _default_user(request)
        item.save()
        return Response(ChecklistItemSerializer(item).data)


class TaskCommentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskCommentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "author"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = (
            TaskComment.objects.select_related("task", "task__project", "author")
            .prefetch_related("mentions__mentioned_user")
            .order_by("created_at")
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "author" not in serializer.validated_data:
            serializer.save(author=_default_user(self.request))
        else:
            serializer.save()

    def perform_update(self, serializer):
        comment = self.get_object()
        user = getattr(self.request, "user", None)
        if user and user.is_authenticated and not user.is_superuser and comment.author_id != user.id:
            raise PermissionDenied("You can only edit your own comments (SRS §7.7).")
        serializer.save()

    def perform_destroy(self, instance):
        user = getattr(self.request, "user", None)
        if user and user.is_authenticated and not user.is_superuser and instance.author_id != user.id:
            raise PermissionDenied("You can only delete your own comments (SRS §7.7).")
        instance.delete()


class CommentMentionViewSet(viewsets.ModelViewSet):
    serializer_class = CommentMentionSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["comment", "mentioned_user"]

    def get_queryset(self):
        qs = CommentMention.objects.select_related("comment", "comment__task__project", "mentioned_user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="comment__task__project__company_id")


class AttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = AttachmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]

    def get_queryset(self):
        return Attachment.objects.select_related("uploaded_by").all()

    def perform_create(self, serializer):
        if "uploaded_by" not in serializer.validated_data:
            serializer.save(uploaded_by=_default_user(self.request))
        else:
            serializer.save()

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        """
        Permission-checked download link endpoint (DB Design §6 & §7 Rule #9).
        """
        attachment = self.get_object()
        return Response(
            {
                "id": attachment.id,
                "file_name": attachment.file_name,
                "mime_type": attachment.mime_type,
                "file_size_bytes": attachment.file_size_bytes,
                "signed_download_url": f"/media/{attachment.storage_key}",
            }
        )


class TaskAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskAttachmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task", "attachment"]

    def get_queryset(self):
        qs = TaskAttachment.objects.select_related("task", "task__project", "attachment", "attachment__uploaded_by").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")


class CommentAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = CommentAttachmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["comment", "attachment"]

    def get_queryset(self):
        qs = CommentAttachment.objects.select_related(
            "comment", "comment__task__project", "attachment", "attachment__uploaded_by"
        ).all()
        return scope_queryset_by_company(qs, self.request, company_lookup="comment__task__project__company_id")
