from rest_framework.permissions import BasePermission
from apps.common.permissions import (  # noqa: F401
    HasRole,
    IsCEO,
    IsCompanyMember,
    get_active_membership,
    get_user_company_ids,
    is_ceo,
)


class IsActiveUser(BasePermission):
    """Allow access only to users whose account is active and not locked."""

    message = "Your account is not active or is locked."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.can_login()


class IsCEOOrSelf(BasePermission):
    """
    Allow CEO to update any user, or a user to update their own profile.
    Only the CEO is allowed to change roles.
    """

    message = "Only the CEO can perform this action or change roles."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if "role" in request.data and not is_ceo(request.user):
            return False
        return True

    def has_object_permission(self, request, view, obj):
        if is_ceo(request.user):
            return True
        if "role" in request.data:
            return False
        return obj.pk == request.user.pk
