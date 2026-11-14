import uuid
from django.conf import settings
from django.db import models


class SystemSetting(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="system_settings",
        db_column="company_id",
    )
    setting_key = models.CharField(max_length=120)
    setting_value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_system_settings",
        db_column="updated_by_id",
    )

    class Meta:
        db_table = "system_settings"
        verbose_name = "System Setting"
        verbose_name_plural = "System Settings"
        ordering = ["setting_key"]

    def __str__(self):
        scope = self.company.code if self.company_id else "GLOBAL"
        return f"[{scope}] {self.setting_key}"


class WorkflowStatus(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workflow_statuses",
        db_column="company_id",
    )
    entity_type = models.CharField(max_length=30)
    status_code = models.CharField(max_length=50)
    display_name = models.CharField(max_length=100)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "workflow_statuses"
        verbose_name = "Workflow Status"
        verbose_name_plural = "Workflow Statuses"
        ordering = ["entity_type", "sort_order"]

    def __str__(self):
        return f"{self.entity_type}: {self.display_name} ({self.status_code})"


class WorkflowTransition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workflow_transitions",
        db_column="company_id",
    )
    entity_type = models.CharField(max_length=30)
    from_status = models.ForeignKey(
        WorkflowStatus,
        on_delete=models.CASCADE,
        related_name="outgoing_transitions",
        db_column="from_status_id",
    )
    to_status = models.ForeignKey(
        WorkflowStatus,
        on_delete=models.CASCADE,
        related_name="incoming_transitions",
        db_column="to_status_id",
    )
    role = models.ForeignKey(
        "rbac.Role",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_transitions",
        db_column="role_id",
    )

    class Meta:
        db_table = "workflow_transitions"
        verbose_name = "Workflow Transition"
        verbose_name_plural = "Workflow Transitions"

    def __str__(self):
        return f"{self.entity_type}: {self.from_status.status_code} -> {self.to_status.status_code}"
