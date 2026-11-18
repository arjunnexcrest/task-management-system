from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied as DrfPermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import User
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.companies.models import Company
from apps.projects.models import Project
from apps.tasks.models import Task
from apps.tasks.selectors import get_task_detail_queryset
from .filters import TaskReviewFilterSet
from .models import TaskReview
from .selectors import (
    get_review_analytics_summary,
    get_review_list_queryset,
    get_review_queue_queryset,
)
from .serializers import ReviewQueueTaskSerializer, TaskReviewSerializer
from .services import (
    VALID_APPROVAL_LEVELS,
    check_and_escalate_overdue_reviews_service,
    configure_approval_chain_service,
    get_approval_chain_rules,
    reassign_reviewer_service,
    resolve_task_approval_chain,
    submit_for_review_service,
    submit_review_decision_service,
)


def _resolve_actor(request, task=None, fallback_reviewer=None):
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        return request.user
    if fallback_reviewer is not None:
        return fallback_reviewer
    if task is not None:
        return task.reviewer or task.project.project_manager or task.created_by
    return User.objects.first()


class TaskReviewViewSet(viewsets.ModelViewSet):
    """
    SRS Module 12 (§14.1–§14.4) & DB Design §4.6, §5, §8:
    Manages review queues, configurable approval chains, approval/rejection/correction decisions,
    reviewer reassignment, reference document attachments, and review SLA escalations.
    Historical reviews are preserved non-destructively (DELETE disabled).
    """

    serializer_class = TaskReviewSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TaskReviewFilterSet
    search_fields = ["task__title", "task__project__code", "comments", "correction_reason", "reviewer__full_name"]
    ordering_fields = ["reviewed_at", "review_round"]

    def get_queryset(self):
        return get_review_list_queryset(self.request)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if self.request:
            is_auth = bool(getattr(self.request, "user", None) and self.request.user.is_authenticated)
            ctx["enforce_actor_permission"] = is_auth
            uploaded = self.request.FILES.getlist("files") or self.request.FILES.getlist("file")
            if uploaded:
                ctx["uploaded_files"] = uploaded
        return ctx

    def perform_create(self, serializer):
        task = serializer.validated_data.get("task")
        if "reviewer" not in serializer.validated_data:
            actor = _resolve_actor(self.request, task=task)
            serializer.save(reviewer=actor)
        else:
            if self.request and getattr(self.request, "user", None) and self.request.user.is_authenticated:
                # Authenticated caller cannot spoof another reviewer unless superuser
                explicit_rev = serializer.validated_data["reviewer"]
                if not self.request.user.is_superuser and explicit_rev.id != self.request.user.id:
                    raise DrfPermissionDenied(
                        "Authenticated reviewers cannot submit decisions on behalf of another user."
                    )
            serializer.save()

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed(
            "DELETE",
            detail="Historical task review records must be preserved for audit integrity (DB Design §5 & SRS §14.3).",
        )

    @action(detail=False, methods=["get"])
    def queue(self, request):
        """
        Screen #33 (Task Review and Approval):
        Returns filtered tasks across review workflow tabs (`pending`, `changes_requested`,
        `approved`, `ready_to_submit`, `all`), enriched with live approval chains and SLA status.
        """
        tab = request.query_params.get("queue_tab") or request.query_params.get("tab") or "pending"
        qs = get_review_queue_queryset(request, queue_tab=tab)

        overdue_only = str(request.query_params.get("overdue_only", "")).lower() in ("true", "1", "yes")
        if overdue_only:
            matched_ids = []
            for t in qs:
                sla = compute_task_sla_safe(t)
                if sla["is_review_overdue"]:
                    matched_ids.append(t.id)
            qs = qs.filter(id__in=matched_ids)

        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(ReviewQueueTaskSerializer(page, many=True).data)
        return Response(ReviewQueueTaskSerializer(qs, many=True).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        SRS §14.4, §16.3 & §16.4:
        Aggregated KPI summary, first-pass approval rate, reviewer workload, and correction trends.
        """
        return Response(get_review_analytics_summary(request))

    @action(detail=False, methods=["post"], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def submit(self, request):
        """
        SRS §2.6, §7.3 & §14.1:
        Submits or resubmits a task for review (`In Review`), auto-selecting Step 1 reviewer
        from the configured approval chain or accepting an explicit reviewer.
        """
        task_id = request.data.get("task")
        task = get_task_detail_queryset(request).filter(id=task_id).first() if task_id else None
        if not task:
            return Response({"error": "Valid `task` UUID is required."}, status=status.HTTP_400_BAD_REQUEST)

        actor_id = request.data.get("actor")
        actor = None
        if request.user and request.user.is_authenticated:
            actor = request.user
        elif actor_id:
            actor = User.objects.filter(id=actor_id).first()
        else:
            actor = task.task_owner or task.created_by

        attachment_ids = request.data.get("attachment_ids")
        if isinstance(attachment_ids, str):
            attachment_ids = [s.strip() for s in attachment_ids.split(",") if s.strip()]
        elif hasattr(request.data, "getlist") and request.data.getlist("attachment_ids"):
            attachment_ids = request.data.getlist("attachment_ids")

        uploaded_files = request.FILES.getlist("files") or request.FILES.getlist("file")
        raw_approval_req = request.data.get("approval_required")
        approval_req = (
            None
            if raw_approval_req in (None, "")
            else str(raw_approval_req).lower() in ("true", "1", "yes")
        )

        try:
            updated_task = submit_for_review_service(
                task=task,
                actor=actor,
                reviewer_id=request.data.get("reviewer") or None,
                notes=request.data.get("notes"),
                attachment_ids=attachment_ids,
                uploaded_files=uploaded_files,
                approval_required=approval_req,
            )
        except DjangoPermissionDenied as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

        refreshed = get_review_queue_queryset(request, queue_tab="all").filter(id=updated_task.id).first() or updated_task
        return Response(ReviewQueueTaskSerializer(refreshed).data, status=status.HTTP_200_OK)

    @action(
        detail=False,
        methods=["post"],
        url_path="submit-decision",
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def submit_decision(self, request):
        """
        SRS §14.1–§14.3:
        Records Approve / Request Changes / Reject decision with comments, correction_reason,
        and optional multipart reference documents (`files`) or `attachment_ids`.
        """
        task_id = request.data.get("task")
        task = get_task_detail_queryset(request).filter(id=task_id).first() if task_id else None
        if not task:
            return Response({"error": "Valid `task` UUID is required."}, status=status.HTTP_400_BAD_REQUEST)

        is_auth = bool(request.user and request.user.is_authenticated)
        if is_auth:
            reviewer = request.user
        else:
            rev_id = request.data.get("reviewer")
            reviewer = User.objects.filter(id=rev_id).first() if rev_id else None
            if not reviewer:
                reviewer = task.reviewer or task.project.project_manager or task.created_by

        attachment_ids = request.data.get("attachment_ids")
        if isinstance(attachment_ids, str):
            attachment_ids = [s.strip() for s in attachment_ids.split(",") if s.strip()]
        elif hasattr(request.data, "getlist") and request.data.getlist("attachment_ids"):
            attachment_ids = request.data.getlist("attachment_ids")

        uploaded_files = request.FILES.getlist("files") or request.FILES.getlist("file")

        try:
            review = submit_review_decision_service(
                task=task,
                reviewer=reviewer,
                decision=request.data.get("decision", ""),
                comments=request.data.get("comments"),
                correction_reason=request.data.get("correction_reason"),
                attachment_ids=attachment_ids,
                uploaded_files=uploaded_files,
                enforce_actor_permission=is_auth,
            )
        except DjangoPermissionDenied as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

        refreshed = self.get_queryset().get(pk=review.pk)
        return Response(TaskReviewSerializer(refreshed).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="reassign-reviewer")
    def reassign_reviewer(self, request):
        """
        SRS §14.2: Reassign a reviewer when permitted.
        """
        task_id = request.data.get("task")
        task = get_task_detail_queryset(request).filter(id=task_id).first() if task_id else None
        if not task:
            return Response({"error": "Valid `task` UUID is required."}, status=status.HTTP_400_BAD_REQUEST)

        is_auth = bool(request.user and request.user.is_authenticated)
        actor = request.user if is_auth else (
            User.objects.filter(id=request.data.get("actor")).first()
            if request.data.get("actor")
            else (task.project.project_manager or task.reviewer or task.created_by)
        )

        try:
            updated_task = reassign_reviewer_service(
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

        refreshed = get_review_queue_queryset(request, queue_tab="all").filter(id=updated_task.id).first() or updated_task
        return Response(ReviewQueueTaskSerializer(refreshed).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get", "post"], url_path="approval-chain")
    def approval_chain(self, request):
        """
        SRS §14.1 & §20.2:
        GET: Retrieves active approval chain configuration for a task, project, or company.
        POST: Configures company/project/task approval chains and escalation intervals.
        """
        if request.method == "GET":
            task_id = request.query_params.get("task")
            project_id = request.query_params.get("project")
            company_id = request.query_params.get("company")

            if task_id:
                task = get_task_detail_queryset(request).filter(id=task_id).first()
                if not task:
                    return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
                return Response(
                    {
                        "valid_levels": list(VALID_APPROVAL_LEVELS),
                        "approval_chain": resolve_task_approval_chain(task),
                    }
                )

            project = Project.objects.select_related("company").filter(id=project_id).first() if project_id else None
            company = Company.objects.filter(id=company_id).first() if company_id else None
            return Response(
                {
                    "valid_levels": list(VALID_APPROVAL_LEVELS),
                    "rules": get_approval_chain_rules(company=company, project=project),
                }
            )

        # POST
        scope = request.data.get("scope", "company")
        task = Task.objects.select_related("project", "project__company").filter(id=request.data.get("task")).first() if request.data.get("task") else None
        project = Project.objects.select_related("company").filter(id=request.data.get("project")).first() if request.data.get("project") else None
        company = Company.objects.filter(id=request.data.get("company")).first() if request.data.get("company") else None
        actor = request.user if (request.user and request.user.is_authenticated) else None

        try:
            result = configure_approval_chain_service(
                scope=scope,
                levels=request.data.get("levels", ["Reviewer"]),
                actor=actor,
                company=company,
                project=project,
                task=task,
                priority_chains=request.data.get("priority_chains"),
                user_overrides=request.data.get("user_overrides"),
                escalation_hours=request.data.get("escalation_hours"),
                allow_reviewer_reassignment=request.data.get("allow_reviewer_reassignment"),
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="escalate-overdue")
    def escalate_overdue(self, request):
        """
        SRS §14.4 Review Escalation:
        Scans tasks awaiting review beyond the configured SLA period and dispatches reminders.
        """
        threshold = request.data.get("threshold_hours")
        parsed_threshold = float(threshold) if threshold not in (None, "") else None
        res = check_and_escalate_overdue_reviews_service(
            company_id=request.data.get("company") or request.query_params.get("company"),
            project_id=request.data.get("project") or request.query_params.get("project"),
            threshold_hours_override=parsed_threshold,
            trigger_notifications=bool(request.data.get("trigger_notifications", True)),
            force_notify=bool(request.data.get("force_notify", False)),
        )
        return Response(res, status=status.HTTP_200_OK)


def compute_task_sla_safe(task: Task) -> dict:
    from .services import compute_task_review_sla

    return compute_task_review_sla(task)
