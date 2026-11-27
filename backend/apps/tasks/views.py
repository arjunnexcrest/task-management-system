from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import (
    MANAGEMENT_ROLES,
    IsActiveUser,
    IsCompanyMember,
    extract_object_company_ids,
    is_ceo,
    user_has_any_role,
)
from apps.organization.models import TeamMembership
from apps.projects.selectors import get_authorized_project_ids
from apps.reviews.permissions import validate_can_review_task
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
from .permissions import CanManageTasks, user_is_task_manager
from .selectors import (
    get_authorized_task_ids,
    get_task_detail_queryset,
    get_task_list_queryset,
    scope_queryset_by_task,
)
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
    reschedule_task_service,
    transition_task_status_service,
)


def _ensure_project_authorized(user, project):
    if is_ceo(user):
        return
    allowed = get_authorized_project_ids(user) or set()
    if project.id not in allowed:
        raise PermissionDenied("You do not have access to this project.")


def _ensure_task_authorized(user, task):
    if is_ceo(user):
        return
    allowed = get_authorized_task_ids(user) or set()
    if task.id not in allowed:
        raise PermissionDenied("You do not have access to this task.")


MANAGER_ONLY_TASK_PATCH_FIELDS = {
    "project",
    "assignee_user_ids",
    "assignee_team_ids",
    "task_owner",
    "reviewer",
}


class TaskViewSet(viewsets.ModelViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TaskFilterSet
    search_fields = ["title", "description", "project__name", "project__code"]
    ordering_fields = ["due_date", "start_date", "priority", "progress", "created_at", "updated_at", "title"]

    def get_permissions(self):
        if self.action in ("destroy", "assign", "bulk_assign", "reschedule"):
            return [IsAuthenticated(), IsActiveUser(), CanManageTasks()]
        return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]

    def get_queryset(self):
        if self.action == "retrieve":
            return get_task_detail_queryset(self.request)
        return get_task_list_queryset(self.request)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return TaskDetailSerializer
        return TaskSerializer

    def perform_create(self, serializer):
        project = serializer.validated_data["project"]
        _ensure_project_authorized(self.request.user, project)
        if is_ceo(self.request.user):
            created_by = serializer.validated_data.get("created_by") or self.request.user
        else:
            supplied = serializer.validated_data.get("created_by")
            if supplied is not None and supplied.pk != self.request.user.pk:
                raise PermissionDenied("You cannot create tasks on behalf of another user.")
            created_by = self.request.user
        serializer.save(created_by=created_by)

    def perform_update(self, serializer):
        instance = serializer.instance
        if not user_is_task_manager(self.request.user, instance):
            data_keys = set(self.request.data.keys()) if isinstance(self.request.data, dict) else set()
            if data_keys & MANAGER_ONLY_TASK_PATCH_FIELDS:
                raise PermissionDenied(
                    "Only managers can reassign tasks or change project, owner, or reviewer."
                )
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
        assigned_by_id = (
            (request.data.get("assigned_by") or request.user.id)
            if is_ceo(request.user)
            else request.user.id
        )
        try:
            assignment = assign_task_service(
                task=task,
                assignment_type=request.data.get("assignment_type", TaskAssignment.AssignmentType.USER),
                user_id=request.data.get("user"),
                team_id=request.data.get("team"),
                assigned_by_id=assigned_by_id,
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
        Strictly validates that EVERY requested task_id is within the caller's authorized scope (PERM-05).
        """
        raw_task_ids = request.data.get("task_ids", [])
        unique_ids = [str(tid) for tid in raw_task_ids if tid]
        if unique_ids:
            try:
                authorized_tasks = list(self.get_queryset().filter(id__in=unique_ids))
            except Exception:
                return Response(
                    {"error": "Invalid task UUID in task_ids."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(authorized_tasks) != len(set(unique_ids)) or not all(
                user_is_task_manager(request.user, t) for t in authorized_tasks
            ):
                return Response(
                    {
                        "detail": "You do not have permission to bulk-assign one or more of the specified tasks."
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            updated = bulk_assign_tasks_service(
                task_ids=raw_task_ids,
                actor=request.user,
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
                actor=request.user,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_task_detail_queryset(request).get(pk=updated.pk)
        return Response(TaskDetailSerializer(refreshed).data)

    @action(detail=True, methods=["post"], url_path="transition-status")
    def transition_status(self, request, pk=None):
        """
        Atomically transitions task status with approval and subtask checks (DB Design §4.6 & §7).
        """
        task = self.get_object()
        new_status = request.data.get("status")
        try:
            updated = transition_task_status_service(
                task=task,
                new_status=new_status,
                actor=request.user,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TaskDetailSerializer(updated).data)

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
            is_mgr = user_is_task_manager(request.user, task)
            supplied_author = serializer.validated_data.get("author")
            if not is_mgr and supplied_author is not None and supplied_author.pk != request.user.pk:
                raise PermissionDenied("You cannot post comments on behalf of another user.")
            serializer.save(author=supplied_author if (is_mgr and supplied_author) else request.user)
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
            is_mgr = user_is_task_manager(request.user, task)
            supplied_author = serializer.validated_data.get("author")
            if not is_mgr and supplied_author is not None and supplied_author.pk != request.user.pk:
                raise PermissionDenied("You cannot submit progress updates on behalf of another user.")
            serializer.save(author=supplied_author if (is_mgr and supplied_author) else request.user)
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
        Enforces reviewer role and blocks self-approval (SRS §9 & PERM-06).
        """
        from apps.reviews.serializers import TaskReviewSerializer

        task = self.get_object()
        if request.method == "POST":
            validate_can_review_task(request.user, task)
            data = request.data.copy()
            data["task"] = str(task.id)
            serializer = TaskReviewSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            if is_ceo(request.user):
                reviewer = serializer.validated_data.get("reviewer") or request.user
            else:
                supplied = serializer.validated_data.get("reviewer")
                if supplied is not None and supplied.pk != request.user.pk:
                    raise PermissionDenied("You cannot submit a review on behalf of another user.")
                reviewer = request.user
            serializer.save(reviewer=reviewer)
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
        Blocks non-manager self-approval and user spoofing (PERM-07).
        """
        from apps.tracking.models import TimeEntry
        from apps.tracking.serializers import TimeEntrySerializer

        task = self.get_object()
        if request.method == "POST":
            data = request.data.copy()
            data["task"] = str(task.id)
            serializer = TimeEntrySerializer(data=data)
            serializer.is_valid(raise_exception=True)
            is_mgr = user_is_task_manager(request.user, task)
            supplied_user = serializer.validated_data.get("user")
            approval_status = serializer.validated_data.get(
                "approval_status", TimeEntry.ApprovalStatus.PENDING
            )
            approved_by = serializer.validated_data.get("approved_by")
            if not is_mgr:
                if supplied_user is not None and supplied_user.pk != request.user.pk:
                    raise PermissionDenied("You can only log time entries for yourself.")
                if approval_status != TimeEntry.ApprovalStatus.PENDING or approved_by is not None:
                    raise PermissionDenied("Only managers can approve or reject time entries.")
                serializer.save(
                    user=request.user,
                    approval_status=TimeEntry.ApprovalStatus.PENDING,
                    approved_by=None,
                )
            else:
                if (
                    approval_status in (TimeEntry.ApprovalStatus.APPROVED, TimeEntry.ApprovalStatus.REJECTED)
                    and approved_by is None
                ):
                    approved_by = request.user
                serializer.save(user=supplied_user or request.user, approved_by=approved_by)
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
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = TaskAssignmentFilterSet
    ordering_fields = ["assigned_at", "ended_at"]

    def get_permissions(self):
        if self.action in ("list", "retrieve", "acknowledge"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageTasks()]

    def get_queryset(self):
        qs = TaskAssignment.objects.select_related("task", "task__project", "user", "team", "assigned_by").all()
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        task = serializer.validated_data["task"]
        _ensure_task_authorized(self.request.user, task)
        if not user_is_task_manager(self.request.user, task):
            raise PermissionDenied("Only managers can assign tasks.")
        assigned_by = (
            (serializer.validated_data.get("assigned_by") or self.request.user)
            if is_ceo(self.request.user)
            else self.request.user
        )
        serializer.save(assigned_by=assigned_by)

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        assignment = self.get_object()
        user = request.user
        is_target_user = assignment.user_id == user.id
        is_target_team_member = bool(
            assignment.team_id
            and TeamMembership.objects.filter(
                team_id=assignment.team_id,
                user=user,
                status=TeamMembership.Status.ACTIVE,
            ).exists()
        )
        if not (is_target_user or is_target_team_member or user_is_task_manager(user, assignment.task)):
            raise PermissionDenied("Only the assigned user, assigned team member, or a manager can acknowledge this assignment.")
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
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task", "depends_on_task", "dependency_type"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageTasks()]

    def get_queryset(self):
        qs = TaskDependency.objects.select_related("task", "task__project", "depends_on_task").all()
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        task = serializer.validated_data["task"]
        dep_task = serializer.validated_data["depends_on_task"]
        _ensure_task_authorized(self.request.user, task)
        _ensure_task_authorized(self.request.user, dep_task)
        if not user_is_task_manager(self.request.user, task):
            raise PermissionDenied("Only managers can create task dependencies.")
        serializer.save()


class TaskTagViewSet(viewsets.ModelViewSet):
    queryset = TaskTag.objects.all()
    serializer_class = TaskTagSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name"]

    def get_permissions(self):
        if self.action in ("list", "retrieve", "create"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageTasks()]


class TaskTagAssignmentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskTagAssignmentSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task", "tag"]

    def get_queryset(self):
        qs = TaskTagAssignment.objects.select_related("task", "task__project", "tag").all()
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        _ensure_task_authorized(self.request.user, serializer.validated_data["task"])
        serializer.save()


class ChecklistViewSet(viewsets.ModelViewSet):
    serializer_class = ChecklistSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task"]

    def get_queryset(self):
        qs = Checklist.objects.select_related("task", "task__project").prefetch_related("items").all()
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        _ensure_task_authorized(self.request.user, serializer.validated_data["task"])
        serializer.save(created_by=self.request.user)


class ChecklistItemViewSet(viewsets.ModelViewSet):
    serializer_class = ChecklistItemSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["checklist", "is_completed"]

    def get_queryset(self):
        qs = ChecklistItem.objects.select_related(
            "checklist", "checklist__task", "checklist__task__project", "completed_by"
        ).all()
        return scope_queryset_by_task(
            qs,
            self.request,
            task_lookup="checklist__task_id",
            company_lookup="checklist__task__project__company_id",
        )

    def perform_create(self, serializer):
        _ensure_task_authorized(self.request.user, serializer.validated_data["checklist"].task)
        serializer.save()

    @action(detail=True, methods=["post"])
    def toggle(self, request, pk=None):
        item = self.get_object()
        item.is_completed = not item.is_completed
        item.completed_at = timezone.now() if item.is_completed else None
        if item.is_completed and not item.completed_by_id:
            item.completed_by = request.user
        item.save()
        return Response(ChecklistItemSerializer(item).data)


class TaskCommentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskCommentSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "author"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = (
            TaskComment.objects.select_related("task", "task__project", "author")
            .prefetch_related("mentions__mentioned_user")
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
            raise PermissionDenied("You cannot post comments on behalf of another user.")
        serializer.save(author=supplied_author if (is_mgr and supplied_author) else self.request.user)

    def perform_update(self, serializer):
        instance = serializer.instance
        if instance.author_id != self.request.user.id and not user_is_task_manager(
            self.request.user, instance.task
        ):
            raise PermissionDenied("You can only edit your own comments.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.author_id != self.request.user.id and not user_is_task_manager(
            self.request.user, instance.task
        ):
            raise PermissionDenied("You can only delete your own comments.")
        instance.delete()


class CommentMentionViewSet(viewsets.ModelViewSet):
    serializer_class = CommentMentionSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["comment", "mentioned_user"]

    def get_queryset(self):
        qs = CommentMention.objects.select_related(
            "comment", "comment__task", "comment__task__project", "mentioned_user"
        ).all()
        return scope_queryset_by_task(
            qs,
            self.request,
            task_lookup="comment__task_id",
            company_lookup="comment__task__project__company_id",
        )

    def perform_create(self, serializer):
        _ensure_task_authorized(self.request.user, serializer.validated_data["comment"].task)
        serializer.save()


class AttachmentViewSet(viewsets.ModelViewSet):
    """
    Company- and project-scoped Attachment API (DB Design §6, §7 Rule #9 & PERM-05).
    Users can only see and download attachments they uploaded or attachments linked
    to tasks/comments/progress updates inside their authorized projects.
    """

    serializer_class = AttachmentSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]

    def get_queryset(self):
        qs = Attachment.objects.select_related("uploaded_by").all()
        user = self.request.user
        if is_ceo(user):
            return qs
        auth_project_ids = get_authorized_project_ids(user) or set()
        return (
            qs.filter(
                Q(uploaded_by=user)
                | Q(task_links__task__project_id__in=auth_project_ids)
                | Q(comment_links__comment__task__project_id__in=auth_project_ids)
                | Q(progress_links__progress_update__task__project_id__in=auth_project_ids)
            )
            .distinct()
        )

    def perform_create(self, serializer):
        if is_ceo(self.request.user):
            uploaded_by = serializer.validated_data.get("uploaded_by") or self.request.user
        else:
            supplied = serializer.validated_data.get("uploaded_by")
            if supplied is not None and supplied.pk != self.request.user.pk:
                raise PermissionDenied("You cannot upload attachments on behalf of another user.")
            uploaded_by = self.request.user
        serializer.save(uploaded_by=uploaded_by)

    def _ensure_can_modify_attachment(self, instance: Attachment):
        user = self.request.user
        if is_ceo(user) or instance.uploaded_by_id == user.id:
            return
        cids = extract_object_company_ids(instance)
        if any(user_has_any_role(user, MANAGEMENT_ROLES, company_id=cid) for cid in cids):
            return
        raise PermissionDenied("Only the uploader or a project/company manager can modify or delete this attachment.")

    def perform_update(self, serializer):
        self._ensure_can_modify_attachment(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._ensure_can_modify_attachment(instance)
        instance.delete()

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
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task", "attachment"]

    def get_queryset(self):
        qs = TaskAttachment.objects.select_related("task", "task__project", "attachment", "attachment__uploaded_by").all()
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        _ensure_task_authorized(self.request.user, serializer.validated_data["task"])
        serializer.save()


class CommentAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = CommentAttachmentSerializer
    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["comment", "attachment"]

    def get_queryset(self):
        qs = CommentAttachment.objects.select_related(
            "comment", "comment__task", "comment__task__project", "attachment", "attachment__uploaded_by"
        ).all()
        return scope_queryset_by_task(
            qs,
            self.request,
            task_lookup="comment__task_id",
            company_lookup="comment__task__project__company_id",
        )

    def perform_create(self, serializer):
        _ensure_task_authorized(self.request.user, serializer.validated_data["comment"].task)
        serializer.save()
