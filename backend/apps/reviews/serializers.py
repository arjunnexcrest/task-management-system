from django.db import transaction
from rest_framework import serializers
from apps.tasks.models import Task
from .models import TaskReview


class TaskReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.CharField(source="reviewer.full_name", read_only=True)
    task_title = serializers.CharField(source="task.title", read_only=True)

    class Meta:
        model = TaskReview
        fields = [
            "id",
            "task",
            "task_title",
            "reviewer",
            "reviewer_name",
            "decision",
            "comments",
            "correction_reason",
            "reviewed_at",
            "review_round",
        ]
        read_only_fields = ["id", "reviewed_at"]
        extra_kwargs = {"reviewer": {"required": False}}

    def validate(self, attrs):
        decision = attrs.get("decision")
        correction_reason = attrs.get("correction_reason")
        if decision in (TaskReview.Decision.CHANGES_REQUESTED, TaskReview.Decision.REJECTED) and not correction_reason:
            raise serializers.ValidationError(
                {"correction_reason": "Correction reason is required when requesting changes or rejecting (DB Design §5)."}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        task = validated_data["task"]
        if "review_round" not in validated_data:
            validated_data["review_round"] = task.reviews.count() + 1
        review = super().create(validated_data)

        if review.decision == TaskReview.Decision.APPROVED:
            task.status = Task.Status.APPROVED
        elif review.decision in (TaskReview.Decision.CHANGES_REQUESTED, TaskReview.Decision.REJECTED):
            task.status = Task.Status.CHANGES_REQUESTED
        task.save(update_fields=["status", "updated_at"])
        return review
