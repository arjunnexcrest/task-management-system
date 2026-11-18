from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from apps.accounts.models import User
from apps.common.serializers import CompanyScopedValidationMixin
from apps.notifications.models import Notification
from apps.organization.models import Team
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
from .services import (
    get_allowed_next_statuses,
    sync_task_associations_service,
    transition_task_status_service,
)

ALLOWED_ATTACHMENT_MIME_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.",
    "image/",
    "text/",
    "application/zip",
    "application/json",
)
MAX_ATTACHMENT_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB


class TaskTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskTag
        fields = ["id", "name"]
        read_only_fields = ["id"]


class TaskTagAssignmentSerializer(serializers.ModelSerializer):
    tag_name = serializers.CharField(source="tag.name", read_only=True)

    class Meta:
        model = TaskTagAssignment
        fields = ["id", "task", "tag", "tag_name"]
        read_only_fields = ["id"]


class TaskAssignmentSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True, default=None)
    user_email = serializers.CharField(source="user.email", read_only=True, default=None)
    team_name = serializers.CharField(source="team.name", read_only=True, default=None)
    assigned_by_name = serializers.CharField(source="assigned_by.full_name", read_only=True, default=None)

    class Meta:
        model = TaskAssignment
        fields = [
            "id",
            "task",
            "user",
            "user_name",
            "user_email",
            "team",
            "team_name",
            "assignment_type",
            "assigned_by",
            "assigned_by_name",
            "assigned_at",
            "ended_at",
            "status",
        ]
        read_only_fields = ["id", "assigned_at"]
        extra_kwargs = {"assigned_by": {"required": False}}

    def validate(self, attrs):
        task = attrs.get("task") or getattr(self.instance, "task", None)
        assignment_type = attrs.get("assignment_type") or getattr(
            self.instance, "assignment_type", TaskAssignment.AssignmentType.USER
        )
        user = attrs.get("user") if "user" in attrs else getattr(self.instance, "user", None)
        team = attrs.get("team") if "team" in attrs else getattr(self.instance, "team", None)

        if assignment_type == TaskAssignment.AssignmentType.USER:
            if not user or team is not None:
                raise serializers.ValidationError(
                    "When assignment_type is 'User', user must be set and team must be null (DB Design §5)."
                )
            if task:
                self.validate_user_in_company(user, task.project.company, field_label="Assignee user")
        elif assignment_type == TaskAssignment.AssignmentType.TEAM:
            if not team or user is not None:
                raise serializers.ValidationError(
                    "When assignment_type is 'Team', team must be set and user must be null (DB Design §5)."
                )
            if task:
                self.validate_team_in_company(team, task.project.company)
        return attrs


class TaskDependencySerializer(serializers.ModelSerializer):
    depends_on_title = serializers.CharField(source="depends_on_task.title", read_only=True)
    depends_on_status = serializers.CharField(source="depends_on_task.status", read_only=True)

    class Meta:
        model = TaskDependency
        fields = [
            "id",
            "task",
            "depends_on_task",
            "depends_on_title",
            "depends_on_status",
            "dependency_type",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        task = attrs.get("task") or getattr(self.instance, "task", None)
        depends_on_task = attrs.get("depends_on_task") or getattr(self.instance, "depends_on_task", None)
        if task and depends_on_task:
            if task.id == depends_on_task.id:
                raise serializers.ValidationError("A task cannot depend on itself (DB Design §5).")
            if task.project_id != depends_on_task.project_id:
                raise serializers.ValidationError(
                    "Dependent tasks must belong to the same project (DB Design §4.4 & §5)."
                )
            # Cycle detection across TaskDependency graph
            visited = {task.id}
            queue = [depends_on_task.id]
            while queue:
                current_id = queue.pop(0)
                if current_id in visited:
                    raise serializers.ValidationError(
                        "Circular task dependency detected; dependency cycles are not allowed (DB Design §5)."
                    )
                visited.add(current_id)
                prereqs = TaskDependency.objects.filter(task_id=current_id).values_list(
                    "depends_on_task_id", flat=True
                )
                queue.extend(prereqs)
        return attrs


class ChecklistItemSerializer(serializers.ModelSerializer):
    completed_by_name = serializers.CharField(source="completed_by.full_name", read_only=True, default=None)

    class Meta:
        model = ChecklistItem
        fields = [
            "id",
            "checklist",
            "title",
            "is_completed",
            "notes",
            "link",
            "sort_order",
            "completed_by",
            "completed_by_name",
            "completed_at",
        ]
        read_only_fields = ["id", "completed_at"]


class ChecklistSerializer(serializers.ModelSerializer):
    items = ChecklistItemSerializer(many=True, read_only=True)
    completion_percentage = serializers.SerializerMethodField()

    class Meta:
        model = Checklist
        fields = [
            "id",
            "task",
            "title",
            "sort_order",
            "created_by",
            "created_at",
            "items",
            "completion_percentage",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"created_by": {"required": False}}

    def get_completion_percentage(self, obj):
        items = list(obj.items.all())
        total = len(items)
        if total == 0:
            return 0.0
        done = sum(1 for item in items if item.is_completed)
        return round((done / total) * 100.0, 2)


class CommentMentionSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    mentioned_user_name = serializers.CharField(source="mentioned_user.full_name", read_only=True)

    class Meta:
        model = CommentMention
        fields = ["id", "comment", "mentioned_user", "mentioned_user_name"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        comment = attrs.get("comment") or getattr(self.instance, "comment", None)
        mentioned_user = attrs.get("mentioned_user") or getattr(self.instance, "mentioned_user", None)
        if comment and mentioned_user:
            self.validate_user_in_company(
                mentioned_user,
                comment.task.project.company,
                field_label="Mentioned user",
            )
        return attrs


class TaskCommentSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.full_name", read_only=True)
    mentions = CommentMentionSerializer(many=True, read_only=True)
    mentioned_user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="List of authorized company User UUIDs mentioned in this comment.",
    )
    replies_count = serializers.SerializerMethodField()

    class Meta:
        model = TaskComment
        fields = [
            "id",
            "task",
            "author",
            "author_name",
            "parent_comment",
            "content",
            "mentions",
            "mentioned_user_ids",
            "replies_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {"author": {"required": False}}

    def get_replies_count(self, obj):
        return obj.replies.count()

    def validate(self, attrs):
        task = attrs.get("task") or getattr(self.instance, "task", None)
        parent_comment = attrs.get("parent_comment")
        if task and parent_comment and parent_comment.task_id != task.id:
            raise serializers.ValidationError("Parent comment must belong to the same task.")
        if task and "mentioned_user_ids" in attrs and attrs["mentioned_user_ids"]:
            for u_obj in User.objects.filter(id__in=attrs["mentioned_user_ids"]):
                self.validate_user_in_company(
                    u_obj, task.project.company, field_label="Mentioned user"
                )
        return attrs

    def _sync_mentions(self, comment: TaskComment, mentioned_user_ids):
        if mentioned_user_ids is None:
            return
        unique_ids = {str(uid) for uid in mentioned_user_ids if uid}
        for uid_str in unique_ids:
            _, created = CommentMention.objects.get_or_create(
                comment=comment,
                mentioned_user_id=uid_str,
            )
            if created and str(comment.author_id) != uid_str:
                Notification.objects.create(
                    user_id=uid_str,
                    notification_type="COMMENT_MENTION",
                    title=f"Mentioned in Task: {comment.task.title}",
                    message=f"{comment.author.full_name} mentioned you: \"{comment.content[:120]}\"",
                    related_project=comment.task.project,
                    related_task=comment.task,
                )

    @transaction.atomic
    def create(self, validated_data):
        mentioned_user_ids = validated_data.pop("mentioned_user_ids", None)
        comment = super().create(validated_data)
        self._sync_mentions(comment, mentioned_user_ids)
        return comment

    @transaction.atomic
    def update(self, instance, validated_data):
        mentioned_user_ids = validated_data.pop("mentioned_user_ids", None)
        comment = super().update(instance, validated_data)
        self._sync_mentions(comment, mentioned_user_ids)
        return comment


class AttachmentSerializer(serializers.ModelSerializer):
    """
    Per DB Design §5 & §7 Rule #9:
    - Validates MIME type and file size on upload.
    - Does NOT expose raw unrestricted `storage_key` on read (`write_only=True`);
      instead returns a permission-checked `download_url`.
    """

    uploaded_by_name = serializers.CharField(source="uploaded_by.full_name", read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = [
            "id",
            "uploaded_by",
            "uploaded_by_name",
            "file_name",
            "storage_key",
            "download_url",
            "mime_type",
            "file_size_bytes",
            "checksum",
            "uploaded_at",
        ]
        read_only_fields = ["id", "uploaded_at", "download_url"]
        extra_kwargs = {
            "storage_key": {"write_only": True},
            "uploaded_by": {"required": False},
        }

    def get_download_url(self, obj):
        return f"/api/tasks/attachments/{obj.id}/download/"

    def validate_mime_type(self, value):
        if not value or not value.startswith(ALLOWED_ATTACHMENT_MIME_PREFIXES):
            raise serializers.ValidationError(f"Unsupported MIME type '{value}'.")
        return value

    def validate_file_size_bytes(self, value):
        if value <= 0 or value > MAX_ATTACHMENT_SIZE_BYTES:
            raise serializers.ValidationError("File size must be between 1 byte and 50 MB.")
        return value


class TaskAttachmentSerializer(serializers.ModelSerializer):
    attachment_detail = AttachmentSerializer(source="attachment", read_only=True)

    class Meta:
        model = TaskAttachment
        fields = ["id", "task", "attachment", "attachment_detail"]
        read_only_fields = ["id"]


class CommentAttachmentSerializer(serializers.ModelSerializer):
    attachment_detail = AttachmentSerializer(source="attachment", read_only=True)

    class Meta:
        model = CommentAttachment
        fields = ["id", "comment", "attachment", "attachment_detail"]
        read_only_fields = ["id"]


class SubtaskNestedSerializer(serializers.ModelSerializer):
    task_owner_name = serializers.CharField(source="task_owner.full_name", read_only=True, default=None)

    class Meta:
        model = Task
        fields = [
            "id",
            "project",
            "parent_task",
            "title",
            "priority",
            "status",
            "start_date",
            "due_date",
            "estimated_effort",
            "progress",
            "task_owner",
            "task_owner_name",
            "created_at",
            "completed_at",
        ]
        read_only_fields = ["id", "created_at", "completed_at"]


class TaskSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    company_id = serializers.UUIDField(source="project.company_id", read_only=True)
    company_name = serializers.CharField(source="project.company.name", read_only=True)
    company_code = serializers.CharField(source="project.company.code", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    project_code = serializers.CharField(source="project.code", read_only=True)
    parent_task_title = serializers.CharField(source="parent_task.title", read_only=True, default=None)
    phase_name = serializers.CharField(source="phase.name", read_only=True, default=None)
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)
    team_name = serializers.CharField(source="team.name", read_only=True, default=None)
    task_owner_name = serializers.CharField(source="task_owner.full_name", read_only=True, default=None)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default=None)
    reviewer_name = serializers.CharField(source="reviewer.full_name", read_only=True, default=None)
    is_subtask = serializers.BooleanField(read_only=True)
    is_overdue = serializers.SerializerMethodField()
    days_remaining = serializers.SerializerMethodField()
    due_status = serializers.SerializerMethodField()
    allowed_transitions = serializers.SerializerMethodField()
    subtasks_count = serializers.SerializerMethodField()
    actual_effort_hours = serializers.SerializerMethodField()
    active_assignments = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()

    # SRS §7.1 Write/Read Associations for Task Creation & Editing
    assignee_user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="List of User UUIDs assigned to this task.",
    )
    assignee_team_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="List of Team UUIDs assigned to this task.",
    )
    tag_names = serializers.ListField(
        child=serializers.CharField(max_length=80),
        required=False,
        write_only=True,
        help_text="List of classification tag names for this task.",
    )
    dependency_task_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="List of prerequisite Task UUIDs within the same project.",
    )
    initial_checklist_items = serializers.ListField(
        child=serializers.CharField(max_length=250),
        required=False,
        write_only=True,
        help_text="Optional list of checklist item titles to create with the task.",
    )

    class Meta:
        model = Task
        fields = [
            "id",
            "company_id",
            "company_name",
            "company_code",
            "project",
            "project_name",
            "project_code",
            "parent_task",
            "parent_task_title",
            "is_subtask",
            "phase",
            "phase_name",
            "department",
            "department_name",
            "team",
            "team_name",
            "title",
            "description",
            "task_owner",
            "task_owner_name",
            "created_by",
            "created_by_name",
            "reviewer",
            "reviewer_name",
            "priority",
            "status",
            "start_date",
            "due_date",
            "estimated_effort",
            "actual_effort_hours",
            "progress",
            "progress_method",
            "approval_required",
            "is_overdue",
            "days_remaining",
            "due_status",
            "allowed_transitions",
            "subtasks_count",
            "active_assignments",
            "tags",
            "assignee_user_ids",
            "assignee_team_ids",
            "tag_names",
            "dependency_task_ids",
            "initial_checklist_items",
            "created_at",
            "updated_at",
            "completed_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "completed_at"]
        extra_kwargs = {"created_by": {"required": False}}

    def validate(self, attrs):
        project = attrs.get("project") or getattr(self.instance, "project", None)
        parent_task = (
            attrs.get("parent_task")
            if "parent_task" in attrs
            else getattr(self.instance, "parent_task", None)
        )
        phase = attrs.get("phase") if "phase" in attrs else getattr(self.instance, "phase", None)
        department = (
            attrs.get("department")
            if "department" in attrs
            else getattr(self.instance, "department", None)
        )
        team = attrs.get("team") if "team" in attrs else getattr(self.instance, "team", None)
        task_owner = (
            attrs.get("task_owner")
            if "task_owner" in attrs
            else getattr(self.instance, "task_owner", None)
        )
        reviewer = (
            attrs.get("reviewer")
            if "reviewer" in attrs
            else getattr(self.instance, "reviewer", None)
        )
        start_date = (
            attrs.get("start_date")
            if "start_date" in attrs
            else getattr(self.instance, "start_date", None)
        )
        due_date = (
            attrs.get("due_date")
            if "due_date" in attrs
            else getattr(self.instance, "due_date", None)
        )
        progress_val = attrs.get("progress")
        if progress_val is not None and (float(progress_val) < 0 or float(progress_val) > 100):
            raise serializers.ValidationError(
                {"progress": "Task progress percentage must be between 0 and 100 (DB Design §5)."}
            )

        if project:
            company = project.company
            if parent_task:
                if self.instance and parent_task.id == self.instance.id:
                    raise serializers.ValidationError({"parent_task": "A task cannot be its own parent."})
                if parent_task.project_id != project.id:
                    raise serializers.ValidationError(
                        {"parent_task": "Parent task and subtask must belong to the same project (DB Design §5)."}
                    )
                # Prevent circular parent_task chain
                if self.instance:
                    anc = parent_task
                    while anc:
                        if anc.id == self.instance.id:
                            raise serializers.ValidationError(
                                {"parent_task": "Circular parent-subtask relationship detected."}
                            )
                        anc = anc.parent_task

                # Enforce configurable subtask nesting depth limit (SRS §8.1 & §20.2)
                from apps.system_settings.models import SystemSetting

                depth_setting = (
                    SystemSetting.objects.filter(company=company, setting_key="subtask_max_depth").first()
                    or SystemSetting.objects.filter(company__isnull=True, setting_key="subtask_max_depth").first()
                )
                max_depth = 3
                if depth_setting and isinstance(depth_setting.setting_value, (int, str)):
                    try:
                        max_depth = int(depth_setting.setting_value)
                    except (TypeError, ValueError):
                        max_depth = 3
                depth = 1
                curr = parent_task.parent_task
                while curr is not None:
                    depth += 1
                    curr = curr.parent_task
                if depth > max_depth:
                    raise serializers.ValidationError(
                        {"parent_task": f"Subtask nesting depth cannot exceed the configured limit of {max_depth} level(s) (SRS §8.1)."}
                    )

            if phase and phase.project_id != project.id:
                raise serializers.ValidationError(
                    {"phase": "Selected phase does not belong to the task's project."}
                )
            if department:
                self.validate_department_in_company(department, company)
            if team:
                self.validate_team_in_company(team, company, department=department)
            if task_owner:
                self.validate_user_in_company(task_owner, company, field_label="Task owner")
            if reviewer:
                self.validate_user_in_company(reviewer, company, field_label="Reviewer")

            # Validate assignee_user_ids belong to the project's company
            if "assignee_user_ids" in attrs and attrs["assignee_user_ids"]:
                for u_obj in User.objects.filter(id__in=attrs["assignee_user_ids"]):
                    self.validate_user_in_company(u_obj, company, field_label="Assignee user")

            # Validate assignee_team_ids belong to the project's company
            if "assignee_team_ids" in attrs and attrs["assignee_team_ids"]:
                for t_obj in Team.objects.filter(id__in=attrs["assignee_team_ids"]):
                    self.validate_team_in_company(t_obj, company)

            # Validate dependency_task_ids belong to the same project and do not form cycles
            if "dependency_task_ids" in attrs and attrs["dependency_task_ids"]:
                dep_ids = set(attrs["dependency_task_ids"])
                if self.instance and self.instance.id in dep_ids:
                    raise serializers.ValidationError(
                        {"dependency_task_ids": "A task cannot depend on itself (DB Design §5)."}
                    )
                dep_tasks = list(Task.objects.filter(id__in=dep_ids))
                if len(dep_tasks) != len(dep_ids):
                    raise serializers.ValidationError(
                        {"dependency_task_ids": "One or more prerequisite tasks do not exist."}
                    )
                for dt in dep_tasks:
                    if dt.project_id != project.id:
                        raise serializers.ValidationError(
                            {"dependency_task_ids": "All prerequisite tasks must belong to the same project (DB Design §5)."}
                        )
                    if self.instance:
                        visited = {self.instance.id}
                        queue = [dt.id]
                        while queue:
                            curr = queue.pop(0)
                            if curr in visited:
                                raise serializers.ValidationError(
                                    {"dependency_task_ids": "Circular task dependency detected (DB Design §5)."}
                                )
                            visited.add(curr)
                            prereqs = TaskDependency.objects.filter(task_id=curr).values_list(
                                "depends_on_task_id", flat=True
                            )
                            queue.extend(prereqs)

        if start_date and due_date and due_date < start_date:
            raise serializers.ValidationError({"due_date": "Due date cannot be earlier than start date."})

        return attrs

    def _extract_association_fields(self, validated_data):
        return {
            "assignee_user_ids": validated_data.pop("assignee_user_ids", None),
            "assignee_team_ids": validated_data.pop("assignee_team_ids", None),
            "tag_names": validated_data.pop("tag_names", None),
            "dependency_task_ids": validated_data.pop("dependency_task_ids", None),
            "initial_checklist_items": validated_data.pop("initial_checklist_items", None),
        }

    @transaction.atomic
    def create(self, validated_data):
        assoc = self._extract_association_fields(validated_data)
        # Auto-assign task_owner from first assignee_user_id if task_owner not explicitly provided
        if not validated_data.get("task_owner") and assoc["assignee_user_ids"]:
            validated_data["task_owner_id"] = assoc["assignee_user_ids"][0]

        task = super().create(validated_data)
        sync_task_associations_service(
            task=task,
            actor_id=task.created_by_id,
            **assoc,
        )
        from apps.projects.services import recalculate_project_progress_service

        recalculate_project_progress_service(project=task.project)
        task.refresh_from_db()
        return task

    @transaction.atomic
    def update(self, instance, validated_data):
        assoc = self._extract_association_fields(validated_data)
        assoc.pop("initial_checklist_items", None)

        requested_status = validated_data.pop("status", None)
        task = super().update(instance, validated_data)
        sync_task_associations_service(
            task=task,
            actor_id=task.created_by_id,
            **assoc,
        )

        if requested_status and requested_status != task.status:
            request = self.context.get("request")
            actor = (
                request.user
                if request and getattr(request, "user", None) and request.user.is_authenticated
                else task.created_by
            )
            try:
                transition_task_status_service(
                    task=task,
                    new_status=requested_status,
                    actor=actor,
                )
            except DjangoValidationError as exc:
                msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
                raise serializers.ValidationError({"status": msg})

        if task.progress_method in (Task.ProgressMethod.SUBTASKS, Task.ProgressMethod.CHECKLIST):
            task.recalculate_auto_progress()

        from apps.projects.services import recalculate_project_progress_service

        recalculate_project_progress_service(project=task.project)
        task.refresh_from_db()
        return task

    def to_representation(self, instance):
        data = super().to_representation(instance)
        active_assigns = [
            a for a in instance.assignments.all() if a.status in ("Active", "Acknowledged")
        ]
        data["assignee_user_ids"] = [
            str(a.user_id) for a in active_assigns if a.assignment_type == "User" and a.user_id
        ]
        data["assignee_team_ids"] = [
            str(a.team_id) for a in active_assigns if a.assignment_type == "Team" and a.team_id
        ]
        data["tag_names"] = [ta.tag.name for ta in instance.tag_assignments.all()]
        data["dependency_task_ids"] = [
            str(dep.depends_on_task_id) for dep in instance.dependencies.all()
        ]
        return data

    def get_is_overdue(self, obj):
        if obj.due_date and obj.status not in [Task.Status.COMPLETED, Task.Status.CANCELLED]:
            return obj.due_date < timezone.now().date()
        return False

    def get_days_remaining(self, obj):
        if not obj.due_date or obj.status in [Task.Status.COMPLETED, Task.Status.CANCELLED]:
            return None
        return (obj.due_date - timezone.now().date()).days

    def get_due_status(self, obj):
        if obj.status in [Task.Status.COMPLETED, Task.Status.CANCELLED]:
            return "Completed"
        if not obj.due_date:
            return "No Due Date"
        diff = (obj.due_date - timezone.now().date()).days
        if diff < 0:
            return "Overdue"
        if diff == 0:
            return "Due Today"
        if diff <= 3:
            return "Due Soon"
        return "On Track"

    def get_allowed_transitions(self, obj):
        return get_allowed_next_statuses(task=obj)

    def get_subtasks_count(self, obj):
        if hasattr(obj, "annotated_subtasks_count"):
            return obj.annotated_subtasks_count
        return obj.subtasks.count()

    def get_actual_effort_hours(self, obj):
        total_mins = sum(
            te.duration_minutes or 0 for te in obj.time_entries.all()
        )
        return round(total_mins / 60.0, 2)

    def get_active_assignments(self, obj):
        assignments = [
            a for a in obj.assignments.all() if a.status in ("Active", "Acknowledged")
        ]
        return [
            {
                "id": a.id,
                "assignment_type": a.assignment_type,
                "user_id": a.user_id,
                "user_name": a.user.full_name if a.user else None,
                "team_id": a.team_id,
                "team_name": a.team.name if a.team else None,
                "status": a.status,
                "assigned_at": a.assigned_at,
            }
            for a in assignments
        ]

    def get_tags(self, obj):
        return [ta.tag.name for ta in obj.tag_assignments.all()]


class TaskDetailSerializer(TaskSerializer):
    """
    Per DB Design §8:
    - Task detail: retrieves task + project + owner + creator + department/team with select_related;
      retrieves assignments, checklists and tags with prefetch_related.
    - Task history (comments, progress updates, reviews, time entries) is also exposed via
      dedicated paginated endpoints (`/api/tasks/{id}/comments/`, `/progress-updates/`, `/reviews/`, `/time-entries/`).
    """

    subtasks = SubtaskNestedSerializer(many=True, read_only=True)
    assignments = TaskAssignmentSerializer(many=True, read_only=True)
    dependencies = TaskDependencySerializer(many=True, read_only=True)
    checklists = ChecklistSerializer(many=True, read_only=True)
    comments = serializers.SerializerMethodField()
    progress_updates = serializers.SerializerMethodField()
    task_attachments = TaskAttachmentSerializer(many=True, read_only=True)
    history_endpoints = serializers.SerializerMethodField()

    class Meta(TaskSerializer.Meta):
        fields = TaskSerializer.Meta.fields + [
            "subtasks",
            "assignments",
            "dependencies",
            "checklists",
            "comments",
            "progress_updates",
            "task_attachments",
            "history_endpoints",
        ]

    def get_comments(self, obj):
        # Return latest 20 comments in detail view; full history is paginated at /api/tasks/{id}/comments/
        recent_comments = obj.comments.all()[:20]
        return TaskCommentSerializer(recent_comments, many=True).data

    def get_progress_updates(self, obj):
        from apps.tracking.serializers import ProgressUpdateSerializer

        recent_updates = obj.progress_updates.all()[:20]
        return ProgressUpdateSerializer(recent_updates, many=True).data

    def get_history_endpoints(self, obj):
        return {
            "comments": f"/api/tasks/{obj.id}/comments/",
            "progress_updates": f"/api/tasks/{obj.id}/progress-updates/",
            "reviews": f"/api/tasks/{obj.id}/reviews/",
            "time_entries": f"/api/tasks/{obj.id}/time-entries/",
        }
