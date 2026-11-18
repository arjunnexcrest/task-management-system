from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied as DrfPermissionDenied

from apps.accounts.models import User
from apps.tasks.models import Task
from apps.tasks.serializers import AttachmentSerializer
from apps.tasks.services import get_allowed_next_statuses
from .models import TaskReview
from .services import (
    compute_task_review_sla,
    resolve_task_approval_chain,
    submit_review_decision_service,
)


class TaskReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.CharField(source="reviewer.full_name", read_only=True)
    reviewer_designation = serializers.CharField(
        source="reviewer.designation", read_only=True, default=None
    )
    task_title = serializers.CharField(source="task.title", read_only=True)
    task_status = serializers.CharField(source="task.status", read_only=True)
    task_owner_name = serializers.CharField(
        source="task.task_owner.full_name", read_only=True, default=None
    )
    project_id = serializers.UUIDField(source="task.project_id", read_only=True)
    project_code = serializers.CharField(source="task.project.code", read_only=True)
    project_name = serializers.CharField(source="task.project.name", read_only=True)
    attachment_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="Optional list of existing Attachment UUIDs to link as reference documents.",
    )
    reference_attachments = serializers.SerializerMethodField()
    approval_chain = serializers.SerializerMethodField()

    class Meta:
        model = TaskReview
        fields = [
            "id",
            "task",
            "task_title",
            "task_status",
            "task_owner_name",
            "project_id",
            "project_code",
            "project_name",
            "reviewer",
            "reviewer_name",
            "reviewer_designation",
            "decision",
            "comments",
            "correction_reason",
            "reviewed_at",
            "review_round",
            "attachment_ids",
            "reference_attachments",
            "approval_chain",
        ]
        read_only_fields = ["id", "reviewed_at", "review_round"]
        extra_kwargs = {"reviewer": {"required": False}}

    def validate(self, attrs):
        decision = attrs.get("decision")
        correction_reason = attrs.get("correction_reason")
        if decision in (TaskReview.Decision.CHANGES_REQUESTED, TaskReview.Decision.REJECTED) and not (
            correction_reason and str(correction_reason).strip()
        ):
            raise serializers.ValidationError(
                {
                    "correction_reason": (
                        "Correction reason is required when requesting changes or rejecting "
                        "(DB Design §5 & SRS §14.2)."
                    )
                }
            )
        return attrs

    def create(self, validated_data):
        attachment_ids = validated_data.pop("attachment_ids", None)
        task = validated_data["task"]
        reviewer = validated_data.get("reviewer")
        if not reviewer:
            reviewer = task.reviewer or task.project.project_manager or task.created_by

        uploaded_files = self.context.get("uploaded_files")
        enforce_perm = bool(self.context.get("enforce_actor_permission", False))

        try:
            return submit_review_decision_service(
                task=task,
                reviewer=reviewer,
                decision=validated_data["decision"],
                comments=validated_data.get("comments"),
                correction_reason=validated_data.get("correction_reason"),
                attachment_ids=attachment_ids,
                uploaded_files=uploaded_files,
                enforce_actor_permission=enforce_perm,
            )
        except DjangoPermissionDenied as exc:
            raise DrfPermissionDenied(str(exc))
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            raise serializers.ValidationError({"detail": msg})

    def get_reference_attachments(self, obj):
        prefix = f"[Review Round #{obj.review_round} "
        matched_attachments = []
        seen_ids = set()
        for comment in obj.task.comments.all():
            if comment.content and comment.content.startswith(prefix):
                for ca in comment.comment_attachments.all():
                    if ca.attachment_id not in seen_ids:
                        seen_ids.add(ca.attachment_id)
                        matched_attachments.append(ca.attachment)
        return AttachmentSerializer(matched_attachments, many=True).data

    def get_approval_chain(self, obj):
        if self.context.get("omit_chain", False):
            return None
        return resolve_task_approval_chain(obj.task)


class ReviewQueueTaskSerializer(serializers.ModelSerializer):
    """
    Screen #33 (Task Review and Approval) & SRS §14.1–§14.4:
    Queue item serializer surfacing approval chains, SLA escalation timing,
    previous review rounds with reference attachments, and quick workflow states.
    """

    company_id = serializers.UUIDField(source="project.company_id", read_only=True)
    company_code = serializers.CharField(source="project.company.code", read_only=True)
    project_code = serializers.CharField(source="project.code", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    project_manager_id = serializers.UUIDField(
        source="project.project_manager_id", read_only=True, default=None
    )
    project_manager_name = serializers.CharField(
        source="project.project_manager.full_name", read_only=True, default=None
    )
    department_name = serializers.CharField(
        source="department.name", read_only=True, default=None
    )
    team_name = serializers.CharField(source="team.name", read_only=True, default=None)
    task_owner_name = serializers.CharField(
        source="task_owner.full_name", read_only=True, default=None
    )
    reviewer_name = serializers.CharField(
        source="reviewer.full_name", read_only=True, default=None
    )
    is_subtask = serializers.BooleanField(read_only=True)
    approval_chain = serializers.SerializerMethodField()
    sla = serializers.SerializerMethodField()
    reviews_count = serializers.SerializerMethodField()
    correction_rounds_count = serializers.SerializerMethodField()
    attachments_count = serializers.SerializerMethodField()
    latest_review = serializers.SerializerMethodField()
    reviews_history = serializers.SerializerMethodField()
    allowed_transitions = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "company_id",
            "company_code",
            "project",
            "project_code",
            "project_name",
            "project_manager_id",
            "project_manager_name",
            "department_name",
            "team_name",
            "is_subtask",
            "title",
            "description",
            "priority",
            "status",
            "progress",
            "due_date",
            "approval_required",
            "task_owner",
            "task_owner_name",
            "reviewer",
            "reviewer_name",
            "approval_chain",
            "sla",
            "reviews_count",
            "correction_rounds_count",
            "attachments_count",
            "latest_review",
            "reviews_history",
            "allowed_transitions",
            "created_at",
            "updated_at",
        ]

    def get_approval_chain(self, obj):
        if not hasattr(obj, "_cached_chain"):
            obj._cached_chain = resolve_task_approval_chain(obj)
        return obj._cached_chain

    def get_sla(self, obj):
        chain = self.get_approval_chain(obj)
        return compute_task_review_sla(obj, threshold_hours=chain.get("escalation_hours"))

    def get_reviews_count(self, obj):
        if hasattr(obj, "annotated_reviews_count"):
            return obj.annotated_reviews_count
        return obj.reviews.count()

    def get_correction_rounds_count(self, obj):
        return sum(
            1
            for r in obj.reviews.all()
            if r.decision in (TaskReview.Decision.CHANGES_REQUESTED, TaskReview.Decision.REJECTED)
        )

    def get_attachments_count(self, obj):
        if hasattr(obj, "annotated_attachments_count"):
            return obj.annotated_attachments_count
        return obj.task_attachments.count()

    def get_latest_review(self, obj):
        reviews = sorted(obj.reviews.all(), key=lambda r: (r.review_round, r.reviewed_at), reverse=True)
        if not reviews:
            return None
        return TaskReviewSerializer(reviews[0], context={"omit_chain": True}).data

    def get_reviews_history(self, obj):
        reviews = sorted(obj.reviews.all(), key=lambda r: (r.review_round, r.reviewed_at), reverse=True)
        return TaskReviewSerializer(reviews, many=True, context={"omit_chain": True}).data

    def get_allowed_transitions(self, obj):
        return get_allowed_next_statuses(task=obj)
