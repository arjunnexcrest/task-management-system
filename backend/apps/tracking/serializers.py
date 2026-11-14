from django.db import transaction
from rest_framework import serializers
from apps.tasks.models import Task
from .models import ProgressAttachment, ProgressUpdate, TimeEntry


class ProgressAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProgressAttachment
        fields = ["id", "progress_update", "attachment"]
        read_only_fields = ["id"]


class ProgressUpdateSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.full_name", read_only=True)
    task_title = serializers.CharField(source="task.title", read_only=True)

    class Meta:
        model = ProgressUpdate
        fields = [
            "id",
            "task",
            "task_title",
            "author",
            "author_name",
            "progress_percentage",
            "work_completed",
            "remaining_work",
            "remarks",
            "blockers",
            "time_spent_minutes",
            "next_action",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"author": {"required": False}}

    def validate_progress_percentage(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("Progress percentage must be between 0 and 100 (DB Design §5).")
        return value

    def validate_time_spent_minutes(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Time spent minutes cannot be negative.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        instance = super().create(validated_data)
        task = instance.task
        # Per DB Design §5: update task progress only when progress_method is Manual
        if task.progress_method == Task.ProgressMethod.MANUAL:
            task.progress = instance.progress_percentage
            task.save(update_fields=["progress", "updated_at"])
        return instance


class TimeEntrySerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    approved_by_name = serializers.CharField(source="approved_by.full_name", read_only=True, default=None)
    task_title = serializers.CharField(source="task.title", read_only=True)

    class Meta:
        model = TimeEntry
        fields = [
            "id",
            "task",
            "task_title",
            "user",
            "user_name",
            "started_at",
            "ended_at",
            "duration_minutes",
            "entry_type",
            "approval_status",
            "approved_by",
            "approved_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"user": {"required": False}}

    def validate(self, attrs):
        started_at = attrs.get("started_at")
        ended_at = attrs.get("ended_at")
        duration = attrs.get("duration_minutes", 0)

        if duration is not None and duration < 0:
            raise serializers.ValidationError({"duration_minutes": "Duration cannot be negative (DB Design §5)."})
        if started_at and ended_at and ended_at <= started_at:
            raise serializers.ValidationError({"ended_at": "Ended time must be after start time (DB Design §5)."})
        return attrs
