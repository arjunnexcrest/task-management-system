from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from apps.accounts.models import User
from apps.common.serializers import CompanyScopedValidationMixin
from apps.tasks.models import Task
from apps.tasks.serializers import AttachmentSerializer
from .models import ProgressAttachment, ProgressUpdate, TimeEntry
from .services import submit_progress_update_service


class ProgressAttachmentSerializer(serializers.ModelSerializer):
    attachment_detail = AttachmentSerializer(source="attachment", read_only=True)

    class Meta:
        model = ProgressAttachment
        fields = ["id", "progress_update", "attachment", "attachment_detail"]
        read_only_fields = ["id"]


class ProgressUpdateSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.full_name", read_only=True)
    task_title = serializers.CharField(source="task.title", read_only=True)
    task_status = serializers.CharField(source="task.status", read_only=True)
    task_priority = serializers.CharField(source="task.priority", read_only=True)
    task_progress = serializers.DecimalField(
        source="task.progress", max_digits=5, decimal_places=2, read_only=True
    )
    task_progress_method = serializers.CharField(source="task.progress_method", read_only=True)
    project_id = serializers.UUIDField(source="task.project_id", read_only=True)
    project_name = serializers.CharField(source="task.project.name", read_only=True)
    project_code = serializers.CharField(source="task.project.code", read_only=True)
    attachments = ProgressAttachmentSerializer(
        source="progress_attachments", many=True, read_only=True
    )

    # Optional write-only orchestration fields (SRS §7.5, §7.6, §9.1, §9.4)
    attachment_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="Optional list of Attachment UUIDs to link via progress_attachments.",
    )
    progress_method = serializers.ChoiceField(
        choices=Task.ProgressMethod.choices,
        required=False,
        write_only=True,
        help_text="Optional override for the task's progress calculation method.",
    )
    mark_task_blocked = serializers.BooleanField(
        required=False,
        default=False,
        write_only=True,
        help_text="When blockers text is provided, optionally transition the task status to Blocked.",
    )
    blocker_category = serializers.CharField(
        max_length=80,
        required=False,
        default="Execution",
        write_only=True,
    )
    blocker_impact = serializers.CharField(
        max_length=30,
        required=False,
        default="High",
        write_only=True,
    )
    blocker_responsible_person = serializers.UUIDField(
        required=False,
        allow_null=True,
        write_only=True,
    )

    class Meta:
        model = ProgressUpdate
        fields = [
            "id",
            "task",
            "task_title",
            "task_status",
            "task_priority",
            "task_progress",
            "task_progress_method",
            "project_id",
            "project_name",
            "project_code",
            "author",
            "author_name",
            "progress_percentage",
            "work_completed",
            "remaining_work",
            "remarks",
            "blockers",
            "time_spent_minutes",
            "next_action",
            "attachments",
            "attachment_ids",
            "progress_method",
            "mark_task_blocked",
            "blocker_category",
            "blocker_impact",
            "blocker_responsible_person",
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

    def validate(self, attrs):
        task = attrs.get("task") or getattr(self.instance, "task", None)
        author = attrs.get("author") or getattr(self.instance, "author", None)
        if task and author:
            self.validate_user_in_company(
                author,
                task.project.company,
                field_label="Progress update author",
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        attachment_ids = validated_data.pop("attachment_ids", None)
        progress_method = validated_data.pop("progress_method", None)
        mark_task_blocked = validated_data.pop("mark_task_blocked", False)
        blocker_category = validated_data.pop("blocker_category", "Execution")
        blocker_impact = validated_data.pop("blocker_impact", "High")
        blocker_responsible_person_id = validated_data.pop("blocker_responsible_person", None)

        author = validated_data.get("author")
        if not author:
            request = self.context.get("request")
            if request and getattr(request, "user", None) and request.user.is_authenticated:
                author = request.user
            else:
                author = validated_data["task"].task_owner or validated_data["task"].created_by or User.objects.first()

        try:
            return submit_progress_update_service(
                task=validated_data["task"],
                author=author,
                progress_percentage=validated_data["progress_percentage"],
                work_completed=validated_data.get("work_completed"),
                remaining_work=validated_data.get("remaining_work"),
                remarks=validated_data.get("remarks"),
                blockers=validated_data.get("blockers"),
                time_spent_minutes=validated_data.get("time_spent_minutes"),
                next_action=validated_data.get("next_action"),
                attachment_ids=attachment_ids,
                progress_method=progress_method,
                mark_task_blocked=mark_task_blocked,
                blocker_category=blocker_category,
                blocker_impact=blocker_impact,
                blocker_responsible_person_id=blocker_responsible_person_id,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            raise serializers.ValidationError({"detail": msg})


class TimeEntrySerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    approved_by_name = serializers.CharField(source="approved_by.full_name", read_only=True, default=None)
    task_title = serializers.CharField(source="task.title", read_only=True)
    project_code = serializers.CharField(source="task.project.code", read_only=True)

    class Meta:
        model = TimeEntry
        fields = [
            "id",
            "task",
            "task_title",
            "project_code",
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
        validators = []

    def validate(self, attrs):
        started_at = attrs.get("started_at")
        ended_at = attrs.get("ended_at")
        duration = attrs.get("duration_minutes", 0)
        entry_type = attrs.get("entry_type", getattr(self.instance, "entry_type", TimeEntry.EntryType.MANUAL))
        task = attrs.get("task") or getattr(self.instance, "task", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)

        if task and user:
            self.validate_user_in_company(user, task.project.company, field_label="Time entry user")
        if duration is not None and duration < 0:
            raise serializers.ValidationError({"duration_minutes": "Duration cannot be negative (DB Design §5)."})
        if started_at and ended_at and ended_at <= started_at:
            raise serializers.ValidationError({"ended_at": "Ended time must be after start time (DB Design §5)."})

        request = self.context.get("request")
        target_user = attrs.get("user") or getattr(self.instance, "user", None) or (
            request.user if request and getattr(request.user, "is_authenticated", False) else None
        )
        if entry_type == TimeEntry.EntryType.TIMER and ended_at is None and target_user is not None:
            active_qs = TimeEntry.objects.filter(
                user=target_user, entry_type=TimeEntry.EntryType.TIMER, ended_at__isnull=True
            )
            if self.instance:
                active_qs = active_qs.exclude(pk=self.instance.pk)
            if active_qs.exists():
                raise serializers.ValidationError(
                    {"entry_type": "User already has an active running timer (DB Design §4.6)."}
                )
        return attrs
