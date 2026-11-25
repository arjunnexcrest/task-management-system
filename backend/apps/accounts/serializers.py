from rest_framework import serializers

from apps.common.permissions import (
    get_ordered_user_roles,
    get_primary_role,
    get_user_capabilities,
)
from apps.companies.models import Company, CompanyMembership
from apps.organization.models import DepartmentMembership, TeamMembership
from apps.rbac.models import MembershipRole, Role

from .models import User


class UserSerializer(serializers.ModelSerializer):
    """
    Per DB Design §7 Rule #9: 'Do not expose password hashes or unrestricted storage keys through serializers.'
    """

    password = serializers.CharField(write_only=True, required=False, min_length=8)
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True, write_only=True)
    company = serializers.UUIDField(required=False, allow_null=True, write_only=True)
    role = serializers.CharField(required=False, allow_blank=True, write_only=True)
    roles = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
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
            "capabilities",
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

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["phone"] = None
        data["role"] = get_primary_role(instance)
        memberships = self._active_memberships(instance)
        data["company"] = str(memberships[0].company_id) if memberships else None
        return data

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

    def get_date_joined(self, obj):
        return obj.created_at.isoformat() if obj.created_at else None

    def get_roles(self, obj):
        return get_ordered_user_roles(obj)

    def get_capabilities(self, obj):
        return get_user_capabilities(obj)

    def get_company_ids(self, obj):
        return [str(m.company_id) for m in self._active_memberships(obj)]

    def get_company_name(self, obj):
        memberships = self._active_memberships(obj)
        return memberships[0].company.name if memberships else None

    def get_memberships(self, obj):
        result = []
        for m in self._active_memberships(obj):
            mr = next(iter(m.membership_roles.all()), None)
            role_code = mr.role.code if mr else get_primary_role(obj)
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

    def _sync_company_and_role(self, user, company_id, role_code):
        membership = None
        if company_id:
            company = Company.objects.filter(id=company_id).first()
            if company:
                membership, _ = CompanyMembership.objects.update_or_create(
                    company=company,
                    user=user,
                    defaults={"status": CompanyMembership.Status.ACTIVE},
                )
        else:
            membership = CompanyMembership.objects.filter(
                user=user, status=CompanyMembership.Status.ACTIVE
            ).first()

        if role_code and membership:
            role_obj, _ = Role.objects.get_or_create(
                code=role_code,
                defaults={"name": role_code, "is_system_role": True},
            )
            MembershipRole.objects.filter(membership=membership).exclude(role=role_obj).delete()
            MembershipRole.objects.get_or_create(membership=membership, role=role_obj)
            if role_code == "CEO" and not user.is_superuser:
                user.is_superuser = True
                user.is_staff = True
                user.save(update_fields=["is_superuser", "is_staff"])

        if hasattr(user, "_cached_active_company_memberships"):
            delattr(user, "_cached_active_company_memberships")
        if hasattr(user, "_cached_rbac_role_codes"):
            delattr(user, "_cached_rbac_role_codes")

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        validated_data.pop("phone", None)
        company_id = validated_data.pop("company", None)
        role_code = validated_data.pop("role", None)
        if not validated_data.get("username") and validated_data.get("email"):
            validated_data["username"] = validated_data["email"].split("@")[0]
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        self._sync_company_and_role(user, company_id, role_code)
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        validated_data.pop("phone", None)
        company_id = validated_data.pop("company", None)
        role_code = validated_data.pop("role", None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        if password:
            instance.set_password(password)
        instance.save()
        if company_id is not None or role_code is not None:
            self._sync_company_and_role(instance, company_id, role_code)
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
