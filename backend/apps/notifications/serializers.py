from rest_framework import serializers
from .models import Notification, NotificationDelivery, NotificationPreference


class NotificationDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationDelivery
        fields = [
            "id",
            "notification",
            "channel",
            "delivery_status",
            "sent_at",
            "error_message",
        ]
        read_only_fields = ["id"]


class NotificationSerializer(serializers.ModelSerializer):
    deliveries = NotificationDeliverySerializer(many=True, read_only=True)
    related_task_title = serializers.CharField(source="related_task.title", read_only=True, default=None)
    related_project_code = serializers.CharField(source="related_project.code", read_only=True, default=None)
    related_project_name = serializers.CharField(source="related_project.name", read_only=True, default=None)

    class Meta:
        model = Notification
        fields = [
            "id",
            "user",
            "notification_type",
            "title",
            "message",
            "related_task",
            "related_task_title",
            "related_project",
            "related_project_code",
            "related_project_name",
            "is_read",
            "created_at",
            "read_at",
            "deliveries",
        ]
        read_only_fields = ["id", "created_at"]


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = [
            "id",
            "user",
            "notification_type",
            "in_app_enabled",
            "email_enabled",
        ]
        read_only_fields = ["id"]
