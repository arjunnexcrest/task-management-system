from rest_framework import serializers
from .models import MembershipRole, Permission, Role, RolePermission


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ["id", "code", "description"]
        read_only_fields = ["id"]


class RolePermissionSerializer(serializers.ModelSerializer):
    permission_code = serializers.CharField(source="permission.code", read_only=True)

    class Meta:
        model = RolePermission
        fields = ["id", "role", "permission", "permission_code"]
        read_only_fields = ["id"]


class RoleSerializer(serializers.ModelSerializer):
    role_permissions = RolePermissionSerializer(many=True, read_only=True)

    class Meta:
        model = Role
        fields = ["id", "name", "code", "description", "is_system_role", "role_permissions"]
        read_only_fields = ["id"]


class MembershipRoleSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source="role.name", read_only=True)
    role_code = serializers.CharField(source="role.code", read_only=True)
    user_full_name = serializers.CharField(source="membership.user.full_name", read_only=True)
    company_code = serializers.CharField(source="membership.company.code", read_only=True)

    class Meta:
        model = MembershipRole
        fields = [
            "id",
            "membership",
            "user_full_name",
            "company_code",
            "role",
            "role_name",
            "role_code",
            "assigned_at",
            "assigned_by",
        ]
        read_only_fields = ["id", "assigned_at"]
