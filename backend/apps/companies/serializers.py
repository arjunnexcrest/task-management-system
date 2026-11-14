from rest_framework import serializers
from .models import Company, CompanyMembership


class CompanySerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = [
            "id",
            "name",
            "code",
            "logo",
            "address",
            "contact_information",
            "status",
            "working_calendar",
            "members_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

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
