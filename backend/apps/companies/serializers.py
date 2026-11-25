from rest_framework import serializers
from .models import Company, CompanyMembership


class CompanySerializer(serializers.ModelSerializer):
    code = serializers.CharField(max_length=50, required=False, allow_blank=True)
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
            "is_active",
            "status",
            "logo",
            "contact_information",
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
        return obj.memberships.filter(status="Active").count()


class CompanyMembershipSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)

    class Meta:
        model = CompanyMembership
        fields = [
            "id",
            "user",
            "user_full_name",
            "user_email",
            "company",
            "company_name",
            "company_code",
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
