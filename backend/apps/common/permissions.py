from rest_framework import permissions
from apps.companies.models import CompanyMembership


class IsCompanyMemberOrAllowAnyInDev(permissions.BasePermission):
    """
    Enforces company-level authorization in backend endpoints (DB Design §1 & §7).
    When a user is authenticated, verifies active CompanyMembership for company-scoped objects.
    """

    def has_permission(self, request, view):
        return True

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return True
        if request.user.is_superuser:
            return True

        company_id = getattr(obj, "company_id", None)
        if company_id is None and hasattr(obj, "project") and obj.project is not None:
            company_id = obj.project.company_id
        if company_id is None and hasattr(obj, "task") and obj.task is not None:
            company_id = obj.task.project.company_id

        if company_id is None:
            return True

        return CompanyMembership.objects.filter(
            company_id=company_id,
            user=request.user,
            status=CompanyMembership.Status.ACTIVE,
        ).exists()
