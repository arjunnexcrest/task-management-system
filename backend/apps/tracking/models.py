import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q


class ProgressUpdate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.PROTECT,
        related_name="progress_updates",
        db_column="task_id",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="progress_updates",
        db_column="author_id",
    )
    progress_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    work_completed = models.TextField(null=True, blank=True)
    remaining_work = models.TextField(null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)
    blockers = models.TextField(null=True, blank=True)
    time_spent_minutes = models.IntegerField(null=True, blank=True)
    next_action = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "progress_updates"
        verbose_name = "Progress Update"
        verbose_name_plural = "Progress Updates"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(progress_percentage__gte=0) & Q(progress_percentage__lte=100),
                name="chk_progress_updates_pct_range",
            ),
        ]
        indexes = [
            models.Index(fields=["task", "-created_at"], name="idx_prog_updates_task_created"),
        ]

    def __str__(self):
        return f"{self.task.title} ({self.progress_percentage}%)"


class ProgressAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    progress_update = models.ForeignKey(
        ProgressUpdate,
        on_delete=models.CASCADE,
        related_name="progress_attachments",
        db_column="progress_update_id",
    )
    attachment = models.ForeignKey(
        "tasks.Attachment",
        on_delete=models.CASCADE,
        related_name="progress_links",
        db_column="attachment_id",
    )

    class Meta:
        db_table = "progress_attachments"
        verbose_name = "Progress Attachment"
        verbose_name_plural = "Progress Attachments"
        constraints = [
            models.UniqueConstraint(
                fields=["progress_update", "attachment"],
                name="uq_progress_attachments",
            ),
        ]

    def __str__(self):
        return f"Progress {self.progress_update_id} -> {self.attachment.file_name}"


class TimeEntry(models.Model):
    class EntryType(models.TextChoices):
        MANUAL = "Manual", "Manual"
        TIMER = "Timer", "Timer"

    class ApprovalStatus(models.TextChoices):
        PENDING = "Pending", "Pending"
        APPROVED = "Approved", "Approved"
        REJECTED = "Rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.PROTECT,
        related_name="time_entries",
        db_column="task_id",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="time_entries",
        db_column="user_id",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.IntegerField(default=0)
    entry_type = models.CharField(
        max_length=20,
        choices=EntryType.choices,
        default=EntryType.MANUAL,
    )
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_time_entries",
        db_column="approved_by_id",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "time_entries"
        verbose_name = "Time Entry"
        verbose_name_plural = "Time Entries"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(duration_minutes__gte=0),
                name="chk_time_entries_non_negative_duration",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(entry_type="Timer", ended_at__isnull=True),
                name="uq_active_timer_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-started_at"], name="idx_time_entries_user_started"),
        ]

    def __str__(self):
        return f"{self.user} on {self.task.title} ({self.duration_minutes}m)"
