import re
import uuid
from rest_framework import serializers
from .models import Company, CompanyMembership


class CompanySerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_null=True, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    code = serializers.CharField(max_length=50, required=False, allow_blank=True)

    class Meta:
        model = Company
        fields = [
            "id",
            "name",
            "code",
            "logo",
            "address",
            "email",
            "phone",
            "contact_information",
            "status",
            "is_active",
            "working_calendar",
            "members_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_members_count(self, obj):
        return obj.memberships.filter(status="Active").count()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        contact = instance.contact_information if isinstance(instance.contact_information, dict) else {}
        data["email"] = contact.get("email")
        data["phone"] = contact.get("phone")
        data["is_active"] = instance.status == Company.Status.ACTIVE
        return data

    def _generate_code(self, name: str) -> str:
        base = re.sub(r"[^A-Z0-9]+", "-", (name or "COMP").upper()).strip("-")[:16] or "COMP"
        candidate = base
        if not Company.objects.filter(code__iexact=candidate).exists():
            return candidate
        return f"{base[:10]}-{uuid.uuid4().hex[:4].upper()}"

    def create(self, validated_data):
        email = validated_data.pop("email", None)
        phone = validated_data.pop("phone", None)
        is_active = validated_data.pop("is_active", None)
        if not validated_data.get("code"):
            validated_data["code"] = self._generate_code(validated_data.get("name", ""))
        else:
            validated_data["code"] = validated_data["code"].strip().upper()

        contact = dict(validated_data.get("contact_information") or {})
        if email is not None:
            contact["email"] = email or None
        if phone is not None:
            contact["phone"] = phone or None
        if contact:
            validated_data["contact_information"] = contact

        if is_active is not None:
            validated_data["status"] = (
                Company.Status.ACTIVE if is_active else Company.Status.INACTIVE
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        email = validated_data.pop("email", serializers.empty)
        phone = validated_data.pop("phone", serializers.empty)
        is_active = validated_data.pop("is_active", serializers.empty)

        if "code" in validated_data and validated_data["code"]:
            validated_data["code"] = validated_data["code"].strip().upper()
        elif "code" in validated_data and not validated_data["code"]:
            validated_data.pop("code")

        if email is not serializers.empty or phone is not serializers.empty:
            contact = dict(
                validated_data.get("contact_information")
                or (instance.contact_information if isinstance(instance.contact_information, dict) else {})
            )
            if email is not serializers.empty:
                contact["email"] = email or None
            if phone is not serializers.empty:
                contact["phone"] = phone or None
            validated_data["contact_information"] = contact

        if is_active is not serializers.empty:
            validated_data["status"] = (
                Company.Status.ACTIVE if is_active else Company.Status.INACTIVE
            )

        return super().update(instance, validated_data)


class CompanyMembershipSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = CompanyMembership
        fields = [
            "id",
            "company",
            "company_name",
            "company_code",
            "user",
            "user_full_name",
            "user_email",
            "status",
            "joined_at",
            "left_at",
        ]
        read_only_fields = ["id", "joined_at"]
