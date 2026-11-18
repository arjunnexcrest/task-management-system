import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Task(models.Model):
    class Priority(models.TextChoices):
        CRITICAL = "Critical", "Critical"
        HIGH = "High", "High"
        MEDIUM = "Medium", "Medium"
        LOW = "Low", "Low"

    class Status(models.TextChoices):
        BACKLOG = "Backlog", "Backlog"
        TO_DO = "To Do", "To Do"
        ASSIGNED = "Assigned", "Assigned"
        IN_PROGRESS = "In Progress", "In Progress"
        ON_HOLD = "On Hold", "On Hold"
        BLOCKED = "Blocked", "Blocked"
        IN_REVIEW = "In Review", "In Review"
        CHANGES_REQUESTED = "Changes Requested", "Changes Requested"
        APPROVED = "Approved", "Approved"
        COMPLETED = "Completed", "Completed"
        CANCELLED = "Cancelled", "Cancelled"

    class ProgressMethod(models.TextChoices):
        MANUAL = "Manual", "Manual"
        SUBTASKS = "Subtasks", "Subtasks"
        CHECKLIST = "Checklist", "Checklist"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="tasks",
        db_column="project_id",
    )
    parent_task = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="subtasks",
        db_column="parent_task_id",
    )
    phase = models.ForeignKey(
        "projects.ProjectPhase",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        db_column="phase_id",
    )
    department = models.ForeignKey(
        "organization.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        db_column="department_id",
    )
    team = models.ForeignKey(
        "organization.Team",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        db_column="team_id",
    )
    title = models.CharField(max_length=250)
    description = models.TextField(null=True, blank=True)
    task_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_tasks",
        db_column="task_owner_id",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_tasks",
        db_column="created_by_id",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_tasks",
        db_column="reviewer_id",
    )
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.TO_DO)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    estimated_effort = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    progress_method = models.CharField(
        max_length=20,
        choices=ProgressMethod.choices,
        default=ProgressMethod.MANUAL,
    )
    approval_required = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tasks"
        verbose_name = "Task"
        verbose_name_plural = "Tasks"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(progress__gte=0) & Q(progress__lte=100),
                name="chk_tasks_progress_range",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("parent_task")),
                name="chk_tasks_no_self_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "status"], name="idx_tasks_project_status"),
            models.Index(fields=["project", "due_date"], name="idx_tasks_project_due_date"),
            models.Index(fields=["department", "status"], name="idx_tasks_dept_status"),
            models.Index(fields=["team", "status"], name="idx_tasks_team_status"),
            models.Index(fields=["parent_task"], name="idx_tasks_parent_task"),
            models.Index(fields=["task_owner", "status"], name="idx_tasks_owner_status"),
        ]

    @property
    def is_subtask(self) -> bool:
        return self.parent_task_id is not None

    def clean(self):
        if self.parent_task_id:
            if self.parent_task_id == self.id:
                raise ValidationError("A task cannot be its own parent.")
            if self.parent_task.project_id != self.project_id:
                raise ValidationError("Parent task and subtask must belong to the same project.")

    def recalculate_auto_progress(self):
        from decimal import Decimal

        if self.progress_method == self.ProgressMethod.SUBTASKS:
            children = list(self.subtasks.exclude(status=self.Status.CANCELLED))
            if children:
                # SRS §7.6: Weighted subtasks (using estimated_effort when configured, else equal weight 1.0)
                weights = [
                    float(c.estimated_effort)
                    if (c.estimated_effort is not None and float(c.estimated_effort) > 0)
                    else 1.0
                    for c in children
                ]
                total_weight = sum(weights)
                if total_weight > 0:
                    weighted_sum = sum(
                        float(c.progress) * w for c, w in zip(children, weights)
                    )
                    self.progress = Decimal(f"{round(weighted_sum / total_weight, 2):.2f}")
                    self.save(update_fields=["progress", "updated_at"])
        elif self.progress_method == self.ProgressMethod.CHECKLIST:
            items = ChecklistItem.objects.filter(checklist__task=self)
            total = items.count()
            if total > 0:
                done = items.filter(is_completed=True).count()
                pct = round((done / total) * 100.0, 2)
                self.progress = Decimal(f"{pct:.2f}")
                self.save(update_fields=["progress", "updated_at"])
        if self.parent_task_id:
            self.parent_task.recalculate_auto_progress()
        elif self.project_id:
            self.project.recalculate_progress()

    def save(self, *args, **kwargs):
        from decimal import Decimal

        if self.status == self.Status.COMPLETED and not self.completed_at:
            self.completed_at = timezone.now()
            self.progress = Decimal("100.00")
        super().save(*args, **kwargs)
        if self.parent_task_id:
            self.parent_task.recalculate_auto_progress()
        elif self.project_id:
            self.project.recalculate_progress()

    def __str__(self):
        prefix = "Subtask" if self.parent_task_id else "Task"
        return f"{prefix}: {self.title} ({self.status})"


class TaskAssignment(models.Model):
    class AssignmentType(models.TextChoices):
        USER = "User", "User"
        TEAM = "Team", "Team"

    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        ACKNOWLEDGED = "Acknowledged", "Acknowledged"
        REASSIGNED = "Reassigned", "Reassigned"
        ENDED = "Ended", "Ended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="assignments",
        db_column="task_id",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="task_assignments",
        db_column="user_id",
    )
    team = models.ForeignKey(
        "organization.Team",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="task_assignments",
        db_column="team_id",
    )
    assignment_type = models.CharField(
        max_length=20,
        choices=AssignmentType.choices,
        default=AssignmentType.USER,
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_task_records",
        db_column="assigned_by_id",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        db_table = "task_assignments"
        verbose_name = "Task Assignment"
        verbose_name_plural = "Task Assignments"
        ordering = ["-assigned_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (Q(assignment_type="User") & Q(user__isnull=False) & Q(team__isnull=True))
                    | (Q(assignment_type="Team") & Q(team__isnull=False) & Q(user__isnull=True))
                ),
                name="chk_task_assignments_target_match",
            ),
            models.UniqueConstraint(
                fields=["task", "user"],
                condition=Q(status__in=["Active", "Acknowledged"], user__isnull=False),
                name="uq_active_user_task_assignment",
            ),
            models.UniqueConstraint(
                fields=["task", "team"],
                condition=Q(status__in=["Active", "Acknowledged"], team__isnull=False),
                name="uq_active_team_task_assignment",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="idx_task_assign_user_status"),
            models.Index(fields=["task", "status"], name="idx_task_assign_task_status"),
        ]

    def end_assignment(self, status=Status.REASSIGNED):
        """Preserves assignment history when a task is reassigned or ended (SRS §22 #11)."""
        self.status = status
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_at"])

    def __str__(self):
        target = self.user if self.user_id else self.team
        return f"{self.task.title} -> {target} ({self.status})"


class TaskDependency(models.Model):
    class DependencyType(models.TextChoices):
        FINISH_TO_START = "Finish-to-Start", "Finish-to-Start"
        START_TO_START = "Start-to-Start", "Start-to-Start"
        FINISH_TO_FINISH = "Finish-to-Finish", "Finish-to-Finish"
        START_TO_FINISH = "Start-to-Finish", "Start-to-Finish"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="dependencies",
        db_column="task_id",
    )
    depends_on_task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="dependent_tasks",
        db_column="depends_on_task_id",
    )
    dependency_type = models.CharField(
        max_length=30,
        choices=DependencyType.choices,
        default=DependencyType.FINISH_TO_START,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "task_dependencies"
        verbose_name = "Task Dependency"
        verbose_name_plural = "Task Dependencies"
        constraints = [
            models.UniqueConstraint(fields=["task", "depends_on_task"], name="uq_task_dependencies"),
            models.CheckConstraint(
                condition=~Q(task=F("depends_on_task")),
                name="chk_task_no_self_dependency",
            ),
        ]
        indexes = [
            models.Index(fields=["depends_on_task"], name="idx_task_deps_depends_on"),
        ]

    def clean(self):
        if self.task_id and self.depends_on_task_id:
            if self.task_id == self.depends_on_task_id:
                raise ValidationError("A task cannot depend on itself.")
            if self.task.project_id != self.depends_on_task.project_id:
                raise ValidationError("Dependent tasks must belong to the same project.")

    def __str__(self):
        return f"{self.task.title} depends on {self.depends_on_task.title}"


class TaskTag(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        db_table = "task_tags"
        verbose_name = "Task Tag"
        verbose_name_plural = "Task Tags"
        ordering = ["name"]

    def __str__(self):
        return self.name


class TaskTagAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="tag_assignments",
        db_column="task_id",
    )
    tag = models.ForeignKey(
        TaskTag,
        on_delete=models.CASCADE,
        related_name="task_assignments",
        db_column="tag_id",
    )

    class Meta:
        db_table = "task_tag_assignments"
        verbose_name = "Task Tag Assignment"
        verbose_name_plural = "Task Tag Assignments"
        constraints = [
            models.UniqueConstraint(fields=["task", "tag"], name="uq_task_tag_assignments"),
        ]

    def __str__(self):
        return f"{self.task.title} [{self.tag.name}]"


class Checklist(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="checklists",
        db_column="task_id",
    )
    title = models.CharField(max_length=200)
    sort_order = models.IntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_checklists",
        db_column="created_by_id",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "checklists"
        verbose_name = "Checklist"
        verbose_name_plural = "Checklists"
        ordering = ["sort_order", "created_at"]

    def __str__(self):
        return f"{self.title} ({self.task.title})"


class ChecklistItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    checklist = models.ForeignKey(
        Checklist,
        on_delete=models.CASCADE,
        related_name="items",
        db_column="checklist_id",
    )
    title = models.CharField(max_length=250)
    is_completed = models.BooleanField(default=False)
    notes = models.TextField(null=True, blank=True)
    link = models.CharField(max_length=1000, null=True, blank=True)
    sort_order = models.IntegerField(default=0)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_checklist_items",
        db_column="completed_by_id",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "checklist_items"
        verbose_name = "Checklist Item"
        verbose_name_plural = "Checklist Items"
        ordering = ["sort_order"]
        indexes = [
            models.Index(fields=["checklist", "sort_order"], name="idx_checklist_items_order"),
        ]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.checklist.task.recalculate_auto_progress()

    def __str__(self):
        return f"[{'X' if self.is_completed else ' '}] {self.title}"


class TaskComment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="comments",
        db_column="task_id",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="task_comments",
        db_column="author_id",
    )
    parent_comment = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
        db_column="parent_comment_id",
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(null=True, blank=True, auto_now=True)

    class Meta:
        db_table = "task_comments"
        verbose_name = "Task Comment"
        verbose_name_plural = "Task Comments"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["task", "-created_at"], name="idx_task_comments_task_created"),
        ]

    def __str__(self):
        return f"Comment by {self.author} on {self.task.title}"


class CommentMention(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comment = models.ForeignKey(
        TaskComment,
        on_delete=models.CASCADE,
        related_name="mentions",
        db_column="comment_id",
    )
    mentioned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="comment_mentions",
        db_column="mentioned_user_id",
    )

    class Meta:
        db_table = "comment_mentions"
        verbose_name = "Comment Mention"
        verbose_name_plural = "Comment Mentions"
        constraints = [
            models.UniqueConstraint(fields=["comment", "mentioned_user"], name="uq_comment_mentions"),
        ]

    def __str__(self):
        return f"@{self.mentioned_user} in comment {self.comment_id}"


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_attachments",
        db_column="uploaded_by_id",
    )
    file_name = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=700)
    mime_type = models.CharField(max_length=150)
    file_size_bytes = models.BigIntegerField()
    checksum = models.CharField(max_length=128, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "attachments"
        verbose_name = "Attachment"
        verbose_name_plural = "Attachments"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["uploaded_by", "-uploaded_at"], name="idx_attachments_user_uploaded"),
        ]

    def __str__(self):
        return self.file_name


class TaskAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="task_attachments",
        db_column="task_id",
    )
    attachment = models.ForeignKey(
        Attachment,
        on_delete=models.CASCADE,
        related_name="task_links",
        db_column="attachment_id",
    )

    class Meta:
        db_table = "task_attachments"
        verbose_name = "Task Attachment"
        verbose_name_plural = "Task Attachments"
        constraints = [
            models.UniqueConstraint(fields=["task", "attachment"], name="uq_task_attachments"),
        ]

    def __str__(self):
        return f"{self.task.title} -> {self.attachment.file_name}"


class CommentAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comment = models.ForeignKey(
        TaskComment,
        on_delete=models.CASCADE,
        related_name="comment_attachments",
        db_column="comment_id",
    )
    attachment = models.ForeignKey(
        Attachment,
        on_delete=models.CASCADE,
        related_name="comment_links",
        db_column="attachment_id",
    )

    class Meta:
        db_table = "comment_attachments"
        verbose_name = "Comment Attachment"
        verbose_name_plural = "Comment Attachments"
        constraints = [
            models.UniqueConstraint(fields=["comment", "attachment"], name="uq_comment_attachments"),
        ]

    def __str__(self):
        return f"Comment {self.comment_id} -> {self.attachment.file_name}"
