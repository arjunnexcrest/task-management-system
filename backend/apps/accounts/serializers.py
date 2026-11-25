from rest_framework import serializers

from apps.companies.models import CompanyMembership
from apps.organization.models import DepartmentMembership, TeamMembership
from apps.rbac.models import MembershipRole

from .models import User


class UserSerializer(serializers.ModelSerializer):
    """
    Per DB Design §7 Rule #9: 'Do not expose password hashes or unrestricted storage keys through serializers.'
    """

    password = serializers.CharField(write_only=True, required=False, min_length=8)
    phone = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()
    company_name = serializers.SerializerMethodField()
    company_ids = serializers.SerializerMethodField()
    memberships = serializers.SerializerMethodField()
    department_names = serializers.SerializerMethodField()
    team_names = serializers.SerializerMethodField()
    date_joined = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "employee_id",
            "full_name",
            "email",
            "username",
            "password",
            "profile_image",
            "designation",
            "phone",
            "account_status",
            "is_active",
            "is_staff",
            "is_superuser",
            "role",
            "roles",
            "company",
            "company_name",
            "company_ids",
            "memberships",
            "department_names",
            "team_names",
            "last_login",
            "date_joined",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "last_login", "created_at", "updated_at"]

    def _active_memberships(self, obj):
        cache_attr = "_cached_active_company_memberships"
        if not hasattr(obj, cache_attr):
            memberships = list(
                CompanyMembership.objects.filter(
                    user=obj, status=CompanyMembership.Status.ACTIVE
                )
                .select_related("company")
                .prefetch_related("membership_roles__role")
                .order_by("joined_at")
            )
            setattr(obj, cache_attr, memberships)
        return getattr(obj, cache_attr)

    def get_phone(self, obj):
        return None

    def get_date_joined(self, obj):
        return obj.created_at.isoformat() if obj.created_at else None

    def get_roles(self, obj):
        role_codes: list[str] = []
        for membership in self._active_memberships(obj):
            for mr in membership.membership_roles.all():
                code = mr.role.code
                if code not in role_codes:
                    role_codes.append(code)
        if not role_codes and obj.is_superuser:
            role_codes.append("CEO")
        return role_codes

    def get_role(self, obj):
        roles = self.get_roles(obj)
        if roles:
            return roles[0]
        return "CEO" if obj.is_superuser else "Employee"

    def get_company_ids(self, obj):
        return [str(m.company_id) for m in self._active_memberships(obj)]

    def get_company(self, obj):
        memberships = self._active_memberships(obj)
        return str(memberships[0].company_id) if memberships else None

    def get_company_name(self, obj):
        memberships = self._active_memberships(obj)
        return memberships[0].company.name if memberships else None

    def get_memberships(self, obj):
        result = []
        for m in self._active_memberships(obj):
            mr = next(iter(m.membership_roles.all()), None)
            role_code = mr.role.code if mr else ("CEO" if obj.is_superuser else "Employee")
            result.append(
                {
                    "id": str(m.id),
                    "company": str(m.company_id),
                    "company_name": m.company.name,
                    "company_code": m.company.code,
                    "role": role_code,
                    "is_active": m.status == CompanyMembership.Status.ACTIVE,
                    "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                }
            )
        return result

    def get_department_names(self, obj):
        return list(
            DepartmentMembership.objects.filter(
                user=obj, status=DepartmentMembership.Status.ACTIVE
            )
            .select_related("department")
            .values_list("department__name", flat=True)
            .distinct()
        )

    def get_team_names(self, obj):
        return list(
            TeamMembership.objects.filter(
                user=obj, status=TeamMembership.Status.ACTIVE
            )
            .select_related("team")
            .values_list("team__name", flat=True)
            .distinct()
        )

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(required=False, allow_blank=False)
    email = serializers.CharField(required=False, allow_blank=False)
    username = serializers.CharField(required=False, allow_blank=False)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        identifier = (
            attrs.get("identifier")
            or attrs.get("email")
            or attrs.get("username")
            or ""
        ).strip()
        if not identifier:
            raise serializers.ValidationError(
                {"identifier": "Email or username is required."}
            )
        attrs["identifier"] = identifier
        return attrs


class MeUpdateSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, write_only=True)

    class Meta:
        model = User
        fields = ["full_name", "designation", "profile_image", "phone"]

    def update(self, instance, validated_data):
        validated_data.pop("phone", None)
        return super().update(instance, validated_data)


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
