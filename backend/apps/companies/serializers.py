import re
import uuid
from rest_framework import serializers
from .models import Company, CompanyMembership


class CompanySerializer(serializers.ModelSerializer):
    code = serializers.CharField(max_length=50, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(max_length=30, required=False, allow_null=True, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    members_count = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = [
            "id",
            "name",
            "code",
            "email",
            "phone",
            "address",
            "logo",
            "contact_information",
            "is_active",
            "status",
            "working_calendar",
            "members_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "members_count"]

    def validate_name(self, value):
        cleaned = value.strip()
        qs = Company.objects.filter(name__iexact=cleaned)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A company with this name already exists.")
        return cleaned

    def get_members_count(self, obj):
        return obj.memberships.filter(
            is_active=True, status=CompanyMembership.Status.ACTIVE
        ).count()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        contact = instance.contact_information if isinstance(instance.contact_information, dict) else {}
        if not data.get("email") and contact.get("email"):
            data["email"] = contact.get("email")
        if not data.get("phone") and contact.get("phone"):
            data["phone"] = contact.get("phone")
        return data

    def _generate_code(self, name: str) -> str:
        base = re.sub(r"[^A-Z0-9]+", "-", (name or "COMP").upper()).strip("-")[:16] or "COMP"
        candidate = base
        if not Company.objects.filter(code__iexact=candidate).exists():
            return candidate
        return f"{base[:10]}-{uuid.uuid4().hex[:4].upper()}"

    def create(self, validated_data):
        if not validated_data.get("code"):
            validated_data["code"] = self._generate_code(validated_data.get("name", ""))
        else:
            validated_data["code"] = validated_data["code"].strip().upper()

        if "email" in validated_data and validated_data["email"] == "":
            validated_data["email"] = None
        if "phone" in validated_data and validated_data["phone"] == "":
            validated_data["phone"] = None

        contact = dict(validated_data.get("contact_information") or {})
        if validated_data.get("email"):
            contact.setdefault("email", validated_data["email"])
        if validated_data.get("phone"):
            contact.setdefault("phone", validated_data["phone"])
        if contact:
            validated_data["contact_information"] = contact

        if "is_active" in validated_data:
            validated_data["status"] = (
                Company.Status.ACTIVE if validated_data["is_active"] else Company.Status.INACTIVE
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "code" in validated_data and validated_data["code"]:
            validated_data["code"] = validated_data["code"].strip().upper()
        elif "code" in validated_data and not validated_data["code"]:
            validated_data.pop("code")

        if "email" in validated_data and validated_data["email"] == "":
            validated_data["email"] = None
        if "phone" in validated_data and validated_data["phone"] == "":
            validated_data["phone"] = None

        if "email" in validated_data or "phone" in validated_data:
            contact = dict(
                validated_data.get("contact_information")
                or (instance.contact_information if isinstance(instance.contact_information, dict) else {})
            )
            if "email" in validated_data:
                contact["email"] = validated_data["email"]
            if "phone" in validated_data:
                contact["phone"] = validated_data["phone"]
            validated_data["contact_information"] = contact

        if "is_active" in validated_data:
            validated_data["status"] = (
                Company.Status.ACTIVE if validated_data["is_active"] else Company.Status.INACTIVE
            )
        elif "status" in validated_data:
            validated_data["is_active"] = validated_data["status"] == Company.Status.ACTIVE

        return super().update(instance, validated_data)


class CompanyMembershipSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_is_active = serializers.BooleanField(source="company.is_active", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)
    user_designation = serializers.CharField(source="user.designation", read_only=True)
    user_is_active = serializers.BooleanField(source="user.is_active", read_only=True)

    class Meta:
        model = CompanyMembership
        fields = [
            "id",
            "user",
            "user_full_name",
            "user_email",
            "user_designation",
            "user_is_active",
            "company",
            "company_name",
            "company_code",
            "company_is_active",
            "role",
            "is_active",
            "status",
            "joined_at",
            "left_at",
        ]
        read_only_fields = ["id", "status", "joined_at", "left_at"]

    def validate(self, attrs):
        user = attrs.get("user", getattr(self.instance, "user", None))
        company = attrs.get("company", getattr(self.instance, "company", None))
        is_active = attrs.get("is_active", getattr(self.instance, "is_active", True))

        if self.instance is not None:
            if "user" in attrs and attrs["user"].pk != self.instance.user_id:
                raise serializers.ValidationError(
                    {"user": "Cannot reassign a membership to a different user."}
                )
            if "company" in attrs and attrs["company"].pk != self.instance.company_id:
                raise serializers.ValidationError(
                    {"company": "Cannot move a membership to a different company."}
                )

        if company and is_active and not company.is_active:
            raise serializers.ValidationError(
                {"company": "Cannot add or activate a membership in an inactive company."}
            )

        if user and is_active and not user.is_active:
            raise serializers.ValidationError(
                {"user": "Cannot add or activate a membership for an inactive user."}
            )

        if user and company:
            qs = CompanyMembership.objects.filter(user=user, company=company)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"detail": "This user already has a membership in this company."}
                )
        return attrs

    def _sync_rbac_role(self, membership: CompanyMembership):
        try:
            from apps.rbac.models import MembershipRole, Role

            role_obj, _ = Role.objects.get_or_create(
                code=membership.role,
                defaults={"name": membership.get_role_display(), "is_system_role": True},
            )
            MembershipRole.objects.filter(membership=membership).exclude(role=role_obj).delete()
            MembershipRole.objects.get_or_create(membership=membership, role=role_obj)
        except Exception:
            pass

    def create(self, validated_data):
        membership = super().create(validated_data)
        self._sync_rbac_role(membership)
        return membership

    def update(self, instance, validated_data):
        membership = super().update(instance, validated_data)
        self._sync_rbac_role(membership)
        return membership
