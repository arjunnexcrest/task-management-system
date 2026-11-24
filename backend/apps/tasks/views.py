from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
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
    analyze_task_dependency_impact,
    assign_task_service,
    build_project_dependency_graph_service,
    bulk_assign_tasks_service,
    check_and_notify_deadlines_service,
    check_attachment_access_permission,
    compute_overdue_summary_service,
    delete_attachment_service,
    delete_task_service,
    get_allowed_next_statuses,
    get_company_working_calendar,
    remove_physical_file_if_unreferenced,
    reschedule_task_service,
    serialize_working_calendar,
    store_and_create_attachment_service,
    transition_task_status_service,
    update_company_working_calendar_service,
)


def _default_user(request, task=None):
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        return request.user
    if task is not None:
        if getattr(task, "task_owner", None):
            return task.task_owner
        if getattr(task, "created_by", None):
            return task.created_by
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
        SRS §7.3, §7.9, §8.3, §14.3 & §22:
        Atomically transitions task status while validating permitted workflow paths,
        responsible owner, prerequisite dependencies (FS/SS/FF/SF), required approvals,
        and subtask completion / authorized overrides.
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
                override_dependencies=bool(request.data.get("override_dependencies", False)),
                override_reason=request.data.get("override_reason"),
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_task_detail_queryset(request).get(pk=updated.pk)
        return Response(TaskDetailSerializer(refreshed).data)

    @action(detail=True, methods=["get"], url_path="dependency-impact")
    def dependency_impact(self, request, pk=None):
        """
        SRS §7.9 & §12.4:
        Returns upstream prerequisite status, schedule conflicts, transitive downstream
        dependent tasks, and manager delay ripple warnings for this task.
        """
        task = self.get_object()
        return Response(analyze_task_dependency_impact(task))

    @action(detail=False, methods=["get"], url_path="dependency-graph")
    def dependency_graph(self, request):
        """
        SRS §7.9 & §12.4:
        Returns project-scoped task dependency DAG (nodes with topological levels & overdue
        metrics, edges with satisfaction & schedule conflict flags, and delay warnings).
        """
        project_id = request.query_params.get("project")
        if not project_id:
            return Response(
                {"error": "Query parameter 'project' (Project UUID) is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        scoped_qs = self.filter_queryset(self.get_queryset())
        return Response(
            build_project_dependency_graph_service(
                project_id=project_id,
                tasks_queryset=scoped_qs,
            )
        )

    @action(detail=False, methods=["get"], url_path="overdue-summary")
    def overdue_summary(self, request):
        """
        SRS §7.10, §10.5, §20.1 & §27.3:
        Returns company/project/team/assignee scoped overdue, due-today, due-soon, and
        dependency-blocked KPIs + categorized task lists respecting company working calendars.
        """
        qs = self.filter_queryset(self.get_queryset())
        return Response(compute_overdue_summary_service(tasks_queryset=qs))

    @action(detail=False, methods=["post"], url_path="check-deadlines")
    def check_deadlines(self, request):
        """
        SRS §7.10, §13.1, §20.2 & §22 (#10, #17):
        Scans open tasks in the authorized scope for overdue tasks, upcoming deadlines,
        and downstream dependency delay risks, dispatching deduplicated notifications.
        """
        qs = self.filter_queryset(self.get_queryset())
        project_id = request.data.get("project") or request.query_params.get("project")
        company_id = request.data.get("company") or request.query_params.get("company")
        if project_id:
            qs = qs.filter(project_id=project_id)
        if company_id:
            qs = qs.filter(project__company_id=company_id)
        raw_dedupe = request.data.get("dedupe_hours", 24)
        try:
            dedupe_hours = max(0, int(raw_dedupe))
        except (TypeError, ValueError):
            dedupe_hours = 24
        return Response(
            check_and_notify_deadlines_service(
                tasks_queryset=qs,
                dedupe_hours=dedupe_hours,
            )
        )

    @action(detail=False, methods=["get", "post"], url_path="working-calendar")
    def working_calendar(self, request):
        """
        SRS §20.1 & §20.2:
        GET: Returns effective company working days, holidays, hours/day, and deadline reminder thresholds.
        POST: Updates the company's working_calendar JSONB & reminder settings.
        """
        from apps.companies.models import Company

        company_id = (
            request.query_params.get("company")
            or request.data.get("company")
            or request.headers.get("X-Company-ID")
        )
        company = (
            Company.objects.filter(id=company_id).first()
            if company_id
            else Company.objects.filter(status=Company.Status.ACTIVE).order_by("name").first()
        )
        if company is None:
            return Response(
                {"error": "No active company found for working calendar configuration."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "POST":
            try:
                updated = update_company_working_calendar_service(
                    company=company,
                    working_days=request.data.get("working_days"),
                    holidays=request.data.get("holidays"),
                    hours_per_day=request.data.get("hours_per_day"),
                    due_soon_days=request.data.get("due_soon_days"),
                    deadline_reminder_days=request.data.get("deadline_reminder_days"),
                    actor=_default_user(request),
                )
            except DjangoValidationError as exc:
                msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
                return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
            return Response(updated)

        return Response(serialize_working_calendar(get_company_working_calendar(company)))

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
        Paginated task progress updates history endpoint per DB Design §8 & SRS §7.5, §9.1.
        """
        from apps.tracking.serializers import ProgressUpdateSerializer

        task = self.get_object()
        if request.method == "POST":
            data = request.data.copy()
            data["task"] = str(task.id)
            serializer = ProgressUpdateSerializer(data=data, context={"request": request})
            serializer.is_valid(raise_exception=True)
            if "author" not in serializer.validated_data:
                serializer.save(author=_default_user(request, task=task))
            else:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        qs = (
            task.progress_updates.select_related("author", "task", "task__project")
            .prefetch_related("progress_attachments__attachment__uploaded_by")
            .order_by("-created_at")
        )
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(ProgressUpdateSerializer(page, many=True).data)
        return Response(ProgressUpdateSerializer(qs, many=True).data)

    @action(
        detail=True,
        methods=["get", "post"],
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def reviews(self, request, pk=None):
        """
        Paginated task review history & decision endpoint per DB Design §8 & SRS §14.2.
        """
        from apps.reviews.serializers import TaskReviewSerializer

        task = self.get_object()
        is_auth = bool(getattr(request, "user", None) and request.user.is_authenticated)
        if request.method == "POST":
            data = request.data.copy()
            data["task"] = str(task.id)
            uploaded = request.FILES.getlist("files") or request.FILES.getlist("file")
            serializer = TaskReviewSerializer(
                data=data,
                context={
                    "request": request,
                    "enforce_actor_permission": is_auth,
                    "uploaded_files": uploaded,
                },
            )
            serializer.is_valid(raise_exception=True)
            if "reviewer" not in serializer.validated_data:
                reviewer = (
                    request.user
                    if is_auth
                    else (
                        task.reviewer
                        or task.project.project_manager
                        or task.created_by
                        or User.objects.first()
                    )
                )
                serializer.save(reviewer=reviewer)
            else:
                serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        qs = (
            task.reviews.select_related("reviewer", "task", "task__project")
            .prefetch_related("task__comments__comment_attachments__attachment__uploaded_by")
            .order_by("-reviewed_at", "-review_round")
        )
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(TaskReviewSerializer(page, many=True).data)
        return Response(TaskReviewSerializer(qs, many=True).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="submit-for-review",
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def submit_for_review(self, request, pk=None):
        """
        SRS §2.6, §7.3 & §14.1:
        Submits a task for review, auto-resolving Step 1 reviewer from configured approval chain
        and notifying the assigned reviewer.
        """
        from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
        from apps.reviews.serializers import ReviewQueueTaskSerializer
        from apps.reviews.services import submit_for_review_service

        task = self.get_object()
        is_auth = bool(getattr(request, "user", None) and request.user.is_authenticated)
        actor = request.user if is_auth else (
            User.objects.filter(id=request.data.get("actor")).first()
            if request.data.get("actor")
            else (task.task_owner or task.created_by)
        )
        attachment_ids = request.data.get("attachment_ids")
        if isinstance(attachment_ids, str):
            attachment_ids = [s.strip() for s in attachment_ids.split(",") if s.strip()]
        elif hasattr(request.data, "getlist") and request.data.getlist("attachment_ids"):
            attachment_ids = request.data.getlist("attachment_ids")
        uploaded = request.FILES.getlist("files") or request.FILES.getlist("file")
        raw_approval_req = request.data.get("approval_required")
        approval_req = (
            None
            if raw_approval_req in (None, "")
            else str(raw_approval_req).lower() in ("true", "1", "yes")
        )
        try:
            submit_for_review_service(
                task=task,
                actor=actor,
                reviewer_id=request.data.get("reviewer") or None,
                notes=request.data.get("notes"),
                attachment_ids=attachment_ids,
                uploaded_files=uploaded,
                approval_required=approval_req,
            )
        except DjangoPermissionDenied as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

        refreshed = get_task_detail_queryset(request).get(pk=task.pk)
        return Response(TaskDetailSerializer(refreshed).data)

    @action(detail=True, methods=["post"], url_path="reassign-reviewer")
    def reassign_reviewer(self, request, pk=None):
        """
        SRS §14.2: Reassigns reviewer on a task when permitted.
        """
        from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
        from apps.reviews.services import reassign_reviewer_service

        task = self.get_object()
        is_auth = bool(getattr(request, "user", None) and request.user.is_authenticated)
        actor = request.user if is_auth else (
            User.objects.filter(id=request.data.get("actor")).first()
            if request.data.get("actor")
            else (task.project.project_manager or task.reviewer or task.created_by)
        )
        try:
            reassign_reviewer_service(
                task=task,
                new_reviewer_id=request.data.get("new_reviewer") or request.data.get("reviewer"),
                actor=actor,
                reason=request.data.get("reason"),
                enforce_actor_permission=is_auth,
            )
        except DjangoPermissionDenied as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

        refreshed = get_task_detail_queryset(request).get(pk=task.pk)
        return Response(TaskDetailSerializer(refreshed).data)

    @action(detail=True, methods=["get", "post"], url_path="review-chain")
    def review_chain(self, request, pk=None):
        """
        SRS §14.1–§14.4:
        GET: Returns this task's live approval chain steps, SLA timer status, and review history.
        POST: Updates this task's custom approval levels / user overrides.
        """
        from apps.reviews.serializers import TaskReviewSerializer
        from apps.reviews.services import (
            VALID_APPROVAL_LEVELS,
            compute_task_review_sla,
            configure_approval_chain_service,
            resolve_task_approval_chain,
        )

        task = self.get_object()
        if request.method == "POST":
            is_auth = bool(getattr(request, "user", None) and request.user.is_authenticated)
            actor = request.user if is_auth else None
            try:
                res = configure_approval_chain_service(
                    scope="task",
                    levels=request.data.get("levels", ["Reviewer"]),
                    actor=actor,
                    task=task,
                    user_overrides=request.data.get("user_overrides"),
                    escalation_hours=request.data.get("escalation_hours"),
                )
            except DjangoValidationError as exc:
                msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
                return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
            return Response(res)

        chain = resolve_task_approval_chain(task)
        sla = compute_task_review_sla(task, threshold_hours=chain.get("escalation_hours"))
        reviews_qs = (
            task.reviews.select_related("reviewer", "task", "task__project")
            .prefetch_related("task__comments__comment_attachments__attachment__uploaded_by")
            .order_by("-reviewed_at", "-review_round")
        )
        return Response(
            {
                "task_id": str(task.id),
                "valid_levels": list(VALID_APPROVAL_LEVELS),
                "approval_chain": chain,
                "sla": sla,
                "reviews": TaskReviewSerializer(reviews_qs, many=True, context={"omit_chain": True}).data,
            }
        )

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

    @action(
        detail=True,
        methods=["get", "post"],
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def attachments(self, request, pk=None):
        """
        SRS §7.8 & DB Design §4.5:
        Lists or uploads single/multiple attachments directly associated with this task.
        """
        task = self.get_object()
        if request.method == "POST":
            uploader = _default_user(request, task=task)
            uploaded_files = request.FILES.getlist("files") or request.FILES.getlist("file")
            created_links = []
            try:
                with transaction.atomic():
                    if uploaded_files:
                        for uf in uploaded_files:
                            att = store_and_create_attachment_service(
                                uploaded_by=uploader,
                                uploaded_file=uf,
                                task=task,
                            )
                            link = TaskAttachment.objects.get(task=task, attachment=att)
                            created_links.append(link)
                    elif isinstance(request.data.get("files"), list) and request.data.get("files"):
                        for item in request.data["files"]:
                            att = store_and_create_attachment_service(
                                uploaded_by=uploader,
                                file_name=item.get("file_name"),
                                mime_type=item.get("mime_type"),
                                file_size_bytes=item.get("file_size_bytes"),
                                file_content_base64=item.get("file_content_base64"),
                                task=task,
                            )
                            link = TaskAttachment.objects.get(task=task, attachment=att)
                            created_links.append(link)
                    else:
                        att = store_and_create_attachment_service(
                            uploaded_by=uploader,
                            file_name=request.data.get("file_name"),
                            mime_type=request.data.get("mime_type"),
                            file_size_bytes=request.data.get("file_size_bytes"),
                            file_content_base64=request.data.get("file_content_base64"),
                            task=task,
                        )
                        link = TaskAttachment.objects.get(task=task, attachment=att)
                        created_links.append(link)
            except DjangoValidationError as exc:
                msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
                return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

            return Response(
                TaskAttachmentSerializer(created_links, many=True).data,
                status=status.HTTP_201_CREATED,
            )

        qs = task.task_attachments.select_related("attachment", "attachment__uploaded_by").order_by(
            "-attachment__uploaded_at"
        )
        return Response(TaskAttachmentSerializer(qs, many=True).data)

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
        qs = TaskDependency.objects.select_related(
            "task",
            "task__project",
            "task__task_owner",
            "depends_on_task",
            "depends_on_task__task_owner",
        ).all()
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
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["uploaded_by", "mime_type"]
    search_fields = ["file_name", "mime_type", "uploaded_by__full_name"]
    ordering_fields = ["uploaded_at", "file_size_bytes", "file_name"]

    def get_queryset(self):
        return (
            Attachment.objects.select_related("uploaded_by")
            .prefetch_related("task_links__task__project", "comment_links", "progress_links")
            .order_by("-uploaded_at")
        )

    def perform_create(self, serializer):
        if "uploaded_by" not in serializer.validated_data:
            task_obj = serializer.validated_data.get("task")
            serializer.save(uploaded_by=_default_user(self.request, task=task_obj))
        else:
            serializer.save()

    def destroy(self, request, *args, **kwargs):
        attachment = self.get_object()
        delete_attachment_service(attachment=attachment, actor=getattr(request, "user", None))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["post"])
    def upload(self, request):
        """
        SRS §7.8 Multiple File Uploads & Association:
        Accepts single or multiple files (`files` / `file` via multipart/form-data or JSON `files` list)
        and links them to optional `task`, `comment`, and/or `progress_update`.
        """
        from apps.tracking.models import ProgressUpdate

        task_id = request.data.get("task")
        comment_id = request.data.get("comment")
        progress_update_id = request.data.get("progress_update")

        task_obj = Task.objects.select_related("project__company").filter(id=task_id).first() if task_id else None
        comment_obj = (
            TaskComment.objects.select_related("task__project__company").filter(id=comment_id).first()
            if comment_id
            else None
        )
        progress_obj = (
            ProgressUpdate.objects.select_related("task__project__company").filter(id=progress_update_id).first()
            if progress_update_id
            else None
        )

        uploader_id = request.data.get("uploaded_by")
        uploader = (
            User.objects.filter(id=uploader_id).first()
            if uploader_id
            else _default_user(
                request,
                task=task_obj or (comment_obj.task if comment_obj else None) or (progress_obj.task if progress_obj else None),
            )
        )

        uploaded_files = request.FILES.getlist("files") or request.FILES.getlist("file")
        created_attachments = []
        try:
            with transaction.atomic():
                if uploaded_files:
                    for uf in uploaded_files:
                        att = store_and_create_attachment_service(
                            uploaded_by=uploader,
                            uploaded_file=uf,
                            task=task_obj,
                            comment=comment_obj,
                            progress_update=progress_obj,
                        )
                        created_attachments.append(att)
                elif isinstance(request.data.get("files"), list) and request.data.get("files"):
                    for item in request.data["files"]:
                        att = store_and_create_attachment_service(
                            uploaded_by=uploader,
                            file_name=item.get("file_name"),
                            mime_type=item.get("mime_type"),
                            file_size_bytes=item.get("file_size_bytes"),
                            file_content_base64=item.get("file_content_base64"),
                            task=task_obj,
                            comment=comment_obj,
                            progress_update=progress_obj,
                        )
                        created_attachments.append(att)
                else:
                    att = store_and_create_attachment_service(
                        uploaded_by=uploader,
                        file_name=request.data.get("file_name"),
                        mime_type=request.data.get("mime_type"),
                        file_size_bytes=request.data.get("file_size_bytes"),
                        file_content_base64=request.data.get("file_content_base64"),
                        task=task_obj,
                        comment=comment_obj,
                        progress_update=progress_obj,
                    )
                    created_attachments.append(att)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "uploaded_count": len(created_attachments),
                "attachments": AttachmentSerializer(created_attachments, many=True).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        """
        SRS §7.8 & DB Design §6 & §7 Rule #9:
        Permission-checked download & inline preview endpoint.
        - Validates company access permission.
        - Streams actual file bytes from MEDIA_ROOT with Content-Disposition (`inline` or `attachment`)
          and SHA-256 header when file exists on disk (unless `?meta=1` is requested).
        """
        attachment = self.get_object()
        check_attachment_access_permission(attachment, getattr(request, "user", None))

        if request.query_params.get("meta") == "1":
            return Response(
                {
                    "id": attachment.id,
                    "file_name": attachment.file_name,
                    "mime_type": attachment.mime_type,
                    "file_size_bytes": attachment.file_size_bytes,
                    "checksum": attachment.checksum,
                    "signed_download_url": f"/media/{attachment.storage_key}",
                }
            )

        abs_path = Path(settings.MEDIA_ROOT) / attachment.storage_key
        is_inline = request.query_params.get("inline") == "1"
        disposition = "inline" if is_inline else "attachment"

        if abs_path.exists() and abs_path.is_file():
            content = abs_path.read_bytes()
            resp = HttpResponse(content, content_type=attachment.mime_type or "application/octet-stream")
            resp["Content-Disposition"] = f'{disposition}; filename="{attachment.file_name}"'
            resp["Content-Length"] = str(len(content))
            if attachment.checksum:
                resp["X-Content-SHA256"] = attachment.checksum
            return resp

        if request.query_params.get("stream") == "1" or is_inline:
            fallback_bytes = f"Attachment metadata: {attachment.file_name}".encode("utf-8")
            resp = HttpResponse(fallback_bytes, content_type=attachment.mime_type or "application/octet-stream")
            resp["Content-Disposition"] = f'{disposition}; filename="{attachment.file_name}"'
            resp["Content-Length"] = str(len(fallback_bytes))
            return resp

        return Response(
            {
                "id": attachment.id,
                "file_name": attachment.file_name,
                "mime_type": attachment.mime_type,
                "file_size_bytes": attachment.file_size_bytes,
                "checksum": attachment.checksum,
                "signed_download_url": f"/media/{attachment.storage_key}",
            }
        )


class TaskAttachmentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskAttachmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["task", "attachment"]

    def get_queryset(self):
        qs = TaskAttachment.objects.select_related(
            "task", "task__project", "attachment", "attachment__uploaded_by"
        ).all()
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_destroy(self, instance):
        attachment = instance.attachment
        check_attachment_access_permission(attachment, getattr(self.request, "user", None))
        instance.delete()
        if (
            not attachment.task_links.exists()
            and not attachment.comment_links.exists()
            and not attachment.progress_links.exists()
        ):
            storage_key = attachment.storage_key
            att_id = attachment.id
            attachment.delete()
            remove_physical_file_if_unreferenced(storage_key, exclude_attachment_id=att_id)


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

    def perform_destroy(self, instance):
        attachment = instance.attachment
        check_attachment_access_permission(attachment, getattr(self.request, "user", None))
        instance.delete()
        if (
            not attachment.task_links.exists()
            and not attachment.comment_links.exists()
            and not attachment.progress_links.exists()
        ):
            storage_key = attachment.storage_key
            att_id = attachment.id
            attachment.delete()
            remove_physical_file_if_unreferenced(storage_key, exclude_attachment_id=att_id)
