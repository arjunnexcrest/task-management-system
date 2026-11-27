from rest_framework.permissions import BasePermission
from apps.common.permissions import (  # noqa: F401
    HasRole,
    IsActiveUser,
    IsCEO,
    IsCompanyMember,
    get_active_membership,
    get_user_company_ids,
    is_ceo,
    is_user_active,
)

PRIVILEGED_USER_FIELDS = {
    "role",
    "company",
    "is_staff",
    "is_active",
    "account_status",
    "is_superuser",
}


class IsCEOOrSelf(BasePermission):
    """
    Allow CEO to update any user, or an active user to update their own non-privileged profile fields.
    Only the CEO is allowed to change roles, company assignment, staff flags, or account status.
    """

    message = "Only the CEO can modify other users or change privileged account fields."

    def _has_privileged_fields(self, request) -> bool:
        data = getattr(request, "data", None)
        if isinstance(data, dict) or hasattr(data, "keys"):
            return bool(set(data.keys()) & PRIVILEGED_USER_FIELDS)
        return False

    def has_permission(self, request, view):
        if not is_user_active(request.user):
            return False
        if not is_ceo(request.user) and self._has_privileged_fields(request):
            return False
        return True

    def has_object_permission(self, request, view, obj):
        if not is_user_active(request.user):
            return False
        if is_ceo(request.user):
            return True
        if self._has_privileged_fields(request):
            return False
        return obj.pk == request.user.pk
