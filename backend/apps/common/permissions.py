"""
common/permissions.py

Reusable RBAC & Multi-Company Permission Foundation:
- Helpers:
    - get_company_id_from_request(request, view=None)
    - get_active_membership(user, company_id=None, request=None)
    - get_user_company_ids(user)
    - is_ceo(user)
- Reusable DRF Permission classes:
    - IsCEO
    - HasRole(roles)
    - IsCompanyMember
    - IsCompanyMemberOrAllowAnyInDev (legacy compatibility for other apps)
"""
from rest_framework import permissions
from apps.companies.models import CompanyMembership
from apps.rbac.roles import SystemRole


def get_company_id_from_request(request, view=None):
    """Extract company UUID from query params, request body, X-Company-ID header, or URL kwargs."""
    if request is None:
        return None
    company_id = (
        request.query_params.get("company")
        or request.query_params.get("company_id")
        or request.headers.get("X-Company-ID")
    )
    if not company_id and hasattr(request, "data") and isinstance(request.data, dict):
        company_id = request.data.get("company") or request.data.get("company_id")
    if not company_id and view is not None:
        company_id = view.kwargs.get("company_id") or view.kwargs.get("company_pk")
    return company_id or None


def get_active_membership(user, company_id=None, request=None):
    """
    Return the user's active CompanyMembership for the given company_id
    (or the company inferred from `request`). Returns None if not a member.
    """
    if not user or not user.is_authenticated:
        return None
    if company_id is None and request is not None:
        company_id = get_company_id_from_request(request)
    if not company_id:
        return None
    try:
        return (
            CompanyMembership.objects.select_related("company")
            .filter(
                user=user,
                company_id=company_id,
                is_active=True,
                status=CompanyMembership.Status.ACTIVE,
            )
            .first()
        )
    except Exception:
        return None


def get_user_company_ids(user) -> set:
    """Return the set of company UUIDs where the user holds an active CompanyMembership."""
    if not user or not user.is_authenticated:
        return set()
    return set(
        CompanyMembership.objects.filter(
            user=user,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
        ).values_list("company_id", flat=True)
    )


def is_ceo(user) -> bool:
    """Return True if the authenticated user is a superuser or holds an active CEO membership."""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    if CompanyMembership.objects.filter(
        user=user,
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
        role__iexact=SystemRole.CEO,
    ).exists():
        return True
    try:
        from apps.rbac.models import MembershipRole
        return MembershipRole.objects.filter(
            membership__user=user,
            membership__is_active=True,
            role__code__iexact=SystemRole.CEO,
        ).exists()
    except Exception:
        return False


class IsCEO(permissions.BasePermission):
    """Allow access only to users with the CEO role (or superuser)."""

    message = "Only the CEO can perform this action."

    def has_permission(self, request, view):
        return is_ceo(request.user)


def HasRole(roles):
    """
    Reusable permission factory: HasRole(["CEO", "DepartmentHead"])
    Checks whether the user holds one of the allowed roles in their active
    CompanyMembership (scoped to the company in the request if provided).
    """
    normalized_roles = {str(r).upper() for r in roles}

    class _HasRolePermission(permissions.BasePermission):
        allowed_roles = normalized_roles
        message = f"Requires one of the following roles: {', '.join(roles)}."

        def has_permission(self, request, view):
            user = request.user
            if not user or not user.is_authenticated:
                return False
            if getattr(user, "is_superuser", False):
                return True

            company_id = get_company_id_from_request(request, view)
            if company_id:
                membership = get_active_membership(user, company_id=company_id)
                if not membership:
                    return False
                return membership.role.upper() in self.allowed_roles

            user_roles = CompanyMembership.objects.filter(
                user=user,
                is_active=True,
                status=CompanyMembership.Status.ACTIVE,
            ).values_list("role", flat=True)
            return any(r.upper() in self.allowed_roles for r in user_roles)

    _HasRolePermission.__name__ = f"HasRole_{'_'.join(sorted(normalized_roles))}"
    return _HasRolePermission


class IsCompanyMember(permissions.BasePermission):
    """
    Verifies that the user has an active CompanyMembership for the company
    specified in the request (query/body/header) or on the target object.
    CEO users bypass company restriction; non-members receive 403.
    """

    message = "You are not an active member of this company."

    def _extract_object_company_ids(self, obj) -> set:
        if obj is None:
            return set()
        if obj.__class__.__name__ == "Company":
            return {obj.id}
        cid = getattr(obj, "company_id", None)
        if cid:
            return {cid}
        if hasattr(obj, "department") and obj.department is not None:
            return {obj.department.company_id}
        if hasattr(obj, "team") and obj.team is not None:
            return {obj.team.department.company_id}
        if hasattr(obj, "project") and obj.project is not None:
            return {obj.project.company_id}
        if hasattr(obj, "task") and obj.task is not None:
            return {obj.task.project.company_id}
        if obj.__class__.__name__ == "User":
            return get_user_company_ids(obj)
        return set()

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True

        company_id = get_company_id_from_request(request, view)
        if company_id:
            return get_active_membership(user, company_id=company_id) is not None

        # User must belong to at least one active company (or be accessing their own profile)
        if getattr(view, "action", None) == "me":
            return True
        return len(get_user_company_ids(user)) > 0

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True

        obj_company_ids = self._extract_object_company_ids(obj)
        if not obj_company_ids:
            return getattr(obj, "pk", None) == user.pk

        user_company_ids = get_user_company_ids(user)
        return bool(obj_company_ids & user_company_ids)


class IsCompanyMemberOrAllowAnyInDev(permissions.BasePermission):
    """
    Legacy compatibility permission used by other modules (projects, tasks, organization).
    """

    def has_permission(self, request, view):
        return True

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return True
        if is_ceo(request.user):
            return True

        company_id = getattr(obj, "company_id", None)
        if company_id is None and hasattr(obj, "project") and obj.project is not None:
            company_id = obj.project.company_id
        if company_id is None and hasattr(obj, "task") and obj.task is not None:
            company_id = obj.task.project.company_id

        if company_id is None:
            return True

        return get_active_membership(request.user, company_id=company_id) is not None
