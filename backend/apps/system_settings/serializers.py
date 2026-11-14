from rest_framework import serializers
from .models import SystemSetting, WorkflowStatus, WorkflowTransition


class SystemSettingSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True, default="GLOBAL")
    updated_by_name = serializers.CharField(source="updated_by.full_name", read_only=True, default=None)

    class Meta:
        model = SystemSetting
        fields = [
            "id",
            "company",
            "company_code",
            "setting_key",
            "setting_value",
            "updated_at",
            "updated_by",
            "updated_by_name",
        ]
        read_only_fields = ["id", "updated_at"]


class WorkflowStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowStatus
        fields = [
            "id",
            "company",
            "entity_type",
            "status_code",
            "display_name",
            "sort_order",
            "is_active",
        ]
        read_only_fields = ["id"]


class WorkflowTransitionSerializer(serializers.ModelSerializer):
    from_status_code = serializers.CharField(source="from_status.status_code", read_only=True)
    to_status_code = serializers.CharField(source="to_status.status_code", read_only=True)
    role_code = serializers.CharField(source="role.code", read_only=True, default=None)

    class Meta:
        model = WorkflowTransition
        fields = [
            "id",
            "company",
            "entity_type",
            "from_status",
            "from_status_code",
            "to_status",
            "to_status_code",
            "role",
            "role_code",
        ]
        read_only_fields = ["id"]
