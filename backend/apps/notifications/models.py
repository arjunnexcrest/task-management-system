import uuid
from django.conf import settings
from django.db import models


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        db_column="user_id",
    )
    notification_type = models.CharField(max_length=60)
    title = models.CharField(max_length=200)
    message = models.TextField()
    related_task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        db_column="related_task_id",
    )
    related_project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        db_column="related_project_id",
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notifications"
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["user", "is_read", "-created_at"],
                name="idx_notif_user_read_created",
            ),
        ]

    def __str__(self):
        return f"{self.title} -> {self.user}"


class NotificationDelivery(models.Model):
    class Channel(models.TextChoices):
        IN_APP = "In-App", "In-App"
        EMAIL = "Email", "Email"

    class DeliveryStatus(models.TextChoices):
        PENDING = "Pending", "Pending"
        SENT = "Sent", "Sent"
        FAILED = "Failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="deliveries",
        db_column="notification_id",
    )
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.IN_APP)
    delivery_status = models.CharField(
        max_length=30,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "notification_deliveries"
        verbose_name = "Notification Delivery"
        verbose_name_plural = "Notification Deliveries"

    def __str__(self):
        return f"{self.notification_id} [{self.channel}: {self.delivery_status}]"


class NotificationPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
        db_column="user_id",
    )
    notification_type = models.CharField(max_length=60)
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "notification_preferences"
        verbose_name = "Notification Preference"
        verbose_name_plural = "Notification Preferences"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "notification_type"],
                name="uq_notification_prefs_user_type",
            ),
        ]

    def __str__(self):
        return f"{self.user} - {self.notification_type}"
