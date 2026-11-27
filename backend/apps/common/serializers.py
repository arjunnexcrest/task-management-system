from rest_framework import serializers


class CompanyScopedValidationMixin:
    """
    Reusable serializer helper to enforce cross-company referential integrity
    as required by Database Design §5 and §7 Rule #6:
    'Validate cross-company relationships in serializers/services; a valid FK alone
    does not prove that two related records belong to the same company.'
    """

    def validate_user_in_company(self, user, company, field_label="User"):
        if user is None or company is None:
            return
        if not getattr(company, "is_active", True):
            raise serializers.ValidationError(
                f"Company '{company.name}' is inactive."
            )
        if not getattr(user, "is_active", True):
            raise serializers.ValidationError(
                f"{field_label} '{user.full_name}' is inactive."
            )
        if getattr(user, "is_superuser", False) or getattr(company, "company_incharge_id", None) == user.id:
            return
        from apps.companies.models import CompanyMembership

        is_member = CompanyMembership.objects.filter(
            company=company,
            user=user,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
            company__is_active=True,
        ).exists()
        if not is_member:
            raise serializers.ValidationError(
                f"{field_label} '{user.full_name}' is not an active member of company '{company.code}'."
            )

    def validate_department_in_company(self, department, company):
        if department is None or company is None:
            return
        if department.company_id != company.id:
            raise serializers.ValidationError(
                f"Department '{department.name}' does not belong to company '{company.code}'."
            )

    def validate_team_in_company(self, team, company, department=None):
        if team is None or company is None:
            return
        if team.department.company_id != company.id:
            raise serializers.ValidationError(
                f"Team '{team.name}' does not belong to company '{company.code}'."
            )
        if department is not None and team.department_id != department.id:
            raise serializers.ValidationError(
                f"Team '{team.name}' does not belong to department '{department.name}'."
            )
