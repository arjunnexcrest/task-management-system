import uuid
from django.conf import settings
from django.db import models


class Blocker(models.Model):
    class ResolutionStatus(models.TextChoices):
        OPEN = "Open", "Open"
        IN_PROGRESS = "In Progress", "In Progress"
        RESOLVED = "Resolved", "Resolved"
        ESCALATED = "Escalated", "Escalated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.PROTECT,
        related_name="blockers",
        db_column="task_id",
    )
    description = models.TextField()
    category = models.CharField(max_length=80)
    impact = models.CharField(max_length=30)
    responsible_person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="responsible_blockers",
        db_column="responsible_person_id",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_blockers",
        db_column="reported_by_id",
    )
    reported_at = models.DateTimeField(auto_now_add=True)
    resolution_status = models.CharField(
        max_length=30,
        choices=ResolutionStatus.choices,
        default=ResolutionStatus.OPEN,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "blockers"
        verbose_name = "Blocker"
        verbose_name_plural = "Blockers"
        ordering = ["-reported_at"]

    def __str__(self):
        return f"Blocker on {self.task.title} ({self.resolution_status})"


class Risk(models.Model):
    class Status(models.TextChoices):
        IDENTIFIED = "Identified", "Identified"
        MITIGATING = "Mitigating", "Mitigating"
        RESOLVED = "Resolved", "Resolved"
        CLOSED = "Closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="risks",
        db_column="project_id",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    probability = models.CharField(max_length=20)
    impact = models.CharField(max_length=20)
    severity = models.CharField(max_length=20)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_risks",
        db_column="owner_id",
    )
    mitigation_plan = models.TextField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.IDENTIFIED)

    class Meta:
        db_table = "risks"
        verbose_name = "Risk"
        verbose_name_plural = "Risks"
        ordering = ["due_date", "title"]

    def __str__(self):
        return f"{self.title} ({self.project.code})"


class Issue(models.Model):
    class Status(models.TextChoices):
        OPEN = "Open", "Open"
        IN_PROGRESS = "In Progress", "In Progress"
        RESOLVED = "Resolved", "Resolved"
        CLOSED = "Closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="issues",
        db_column="project_id",
    )
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issues",
        db_column="task_id",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    priority = models.CharField(max_length=20)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_issues",
        db_column="owner_id",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_issues",
        db_column="reported_by_id",
    )
    reported_at = models.DateTimeField(auto_now_add=True)
    due_date = models.DateField(null=True, blank=True)
    resolution_notes = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.OPEN)

    class Meta:
        db_table = "issues"
        verbose_name = "Issue"
        verbose_name_plural = "Issues"
        ordering = ["-reported_at"]

    def __str__(self):
        return f"{self.title} ({self.project.code})"
