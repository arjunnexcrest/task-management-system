"""
common/permissions.py

Reusable RBAC & Multi-Company Permission Foundation:
- Helpers:
    - is_user_active(user)
    - get_company_id_from_request(request, view=None)
    - get_active_membership(user, company_id=None, request=None)
    - get_user_company_ids(user)
    - get_user_roles_in_company(user, company_id=None)
    - user_has_any_role(user, roles, company_id=None)
    - extract_object_company_ids(obj)
    - is_ceo(user)
- Reusable DRF Permission classes:
    - IsActiveUser
    - IsCEO
    - HasRole(roles)
    - IsCompanyMember
    - IsCompanyMemberOrAllowAnyInDev (hardened alias of IsCompanyMember)
"""
from rest_framework import permissions
from apps.companies.models import CompanyMembership
from apps.rbac.roles import SystemRole


MANAGEMENT_ROLES = {
    SystemRole.CEO.upper(),
    SystemRole.COMPANY_IN_CHARGE.upper(),
    SystemRole.PROJECT_MANAGER.upper(),
    SystemRole.DEPARTMENT_HEAD.upper(),
    SystemRole.TEAM_LEADER.upper(),
}

GLOBAL_READONLY_MODELS = {
    "Role",
    "Permission",
    "RolePermission",
    "TaskTag",
}


def is_user_active(user) -> bool:
    """Return True only when user is authenticated, active, and not locked (SRS §3.4)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    can_login = getattr(user, "can_login", None)
    if callable(can_login):
        return bool(can_login())
    return bool(getattr(user, "is_active", False))


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
    (or the company inferred from `request`). Returns None if not an active member.
    """
    if not is_user_active(user):
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
                company__is_active=True,
            )
            .first()
        )
    except Exception:
        return None


def get_user_company_ids(user) -> set:
    """Return the set of active company UUIDs where the user holds an active CompanyMembership."""
    if not is_user_active(user):
        return set()
    return set(
        CompanyMembership.objects.filter(
            user=user,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
            company__is_active=True,
        ).values_list("company_id", flat=True)
    )


def is_ceo(user) -> bool:
    """Return True if the active authenticated user is a superuser or holds an active CEO membership."""
    if not is_user_active(user):
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
            membership__status=CompanyMembership.Status.ACTIVE,
            role__code__iexact=SystemRole.CEO,
        ).exists()
    except Exception:
        return False


def get_user_roles_in_company(user, company_id=None) -> set[str]:
    """
    Return uppercase role codes held by `user` across active company memberships
    (optionally restricted to `company_id`), checking both CompanyMembership.role and MembershipRole.
    """
    if not is_user_active(user):
        return set()

    qs = CompanyMembership.objects.filter(
        user=user,
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
        company__is_active=True,
    )
    if company_id is not None:
        qs = qs.filter(company_id=company_id)

    roles = {str(r).upper() for r in qs.values_list("role", flat=True) if r}
    try:
        from apps.rbac.models import MembershipRole

        mr_qs = MembershipRole.objects.filter(
            membership__user=user,
            membership__is_active=True,
            membership__status=CompanyMembership.Status.ACTIVE,
            membership__company__is_active=True,
        )
        if company_id is not None:
            mr_qs = mr_qs.filter(membership__company_id=company_id)
        roles.update(str(c).upper() for c in mr_qs.values_list("role__code", flat=True) if c)
    except Exception:
        pass
    return roles


def user_has_any_role(user, allowed_roles, company_id=None) -> bool:
    """Return True if active user is CEO or holds any role in `allowed_roles` (scoped to `company_id` if given)."""
    if not is_user_active(user):
        return False
    if is_ceo(user):
        return True
    normalized = {str(r).upper() for r in allowed_roles}
    return bool(get_user_roles_in_company(user, company_id=company_id) & normalized)


def extract_object_company_ids(obj) -> set:
    """Resolve the company UUID(s) associated with any TMS domain object."""
    if obj is None:
        return set()
    cls_name = obj.__class__.__name__
    if cls_name == "Company":
        return {obj.id}

    cid = getattr(obj, "company_id", None)
    if cid:
        return {cid}

    membership = getattr(obj, "membership", None)
    if membership is not None and getattr(membership, "company_id", None):
        return {membership.company_id}

    department = getattr(obj, "department", None)
    if department is not None and getattr(department, "company_id", None):
        return {department.company_id}

    team = getattr(obj, "team", None)
    if team is not None and getattr(team, "department", None) is not None:
        return {team.department.company_id}

    project = getattr(obj, "project", None)
    if project is not None and getattr(project, "company_id", None):
        return {project.company_id}

    milestone = getattr(obj, "milestone", None)
    if milestone is not None and getattr(milestone, "project", None) is not None:
        return {milestone.project.company_id}

    task = getattr(obj, "task", None)
    if task is not None and getattr(task, "project", None) is not None:
        return {task.project.company_id}

    checklist = getattr(obj, "checklist", None)
    if checklist is not None and getattr(checklist, "task", None) is not None:
        return {checklist.task.project.company_id}

    comment = getattr(obj, "comment", None)
    if comment is not None and getattr(comment, "task", None) is not None:
        return {comment.task.project.company_id}

    progress_update = getattr(obj, "progress_update", None)
    if progress_update is not None and getattr(progress_update, "task", None) is not None:
        return {progress_update.task.project.company_id}

    if cls_name == "Attachment":
        cids = set()
        cids.update(obj.task_links.values_list("task__project__company_id", flat=True))
        cids.update(obj.comment_links.values_list("comment__task__project__company_id", flat=True))
        cids.update(
            obj.progress_links.values_list("progress_update__task__project__company_id", flat=True)
        )
        cids.discard(None)
        if cids:
            return cids
        if getattr(obj, "uploaded_by", None) is not None:
            return get_user_company_ids(obj.uploaded_by)
        return set()

    if cls_name == "User":
        return get_user_company_ids(obj)

    return set()


class IsActiveUser(permissions.BasePermission):
    """Allow access only to users whose account is active and not locked."""

    message = "Your account is not active or is locked."

    def has_permission(self, request, view):
        return is_user_active(request.user)


class IsCEO(permissions.BasePermission):
    """Allow access only to active users with the CEO role (or superuser)."""

    message = "Only the CEO can perform this action."

    def has_permission(self, request, view):
        return is_ceo(request.user)

    def has_object_permission(self, request, view, obj):
        return is_ceo(request.user)


def HasRole(roles):
    """
    Reusable permission factory: HasRole(["CEO", "DepartmentHead"])
    Checks whether the active user holds one of the allowed roles in their active
    CompanyMembership (scoped to the company in the request AND on the target object).
    """
    normalized_roles = {str(r).upper() for r in roles}

    class _HasRolePermission(permissions.BasePermission):
        allowed_roles = normalized_roles
        message = f"Requires one of the following roles: {', '.join(roles)}."

        def has_permission(self, request, view):
            user = request.user
            if not is_user_active(user):
                return False
            if is_ceo(user):
                return True

            company_id = get_company_id_from_request(request, view)
            if company_id:
                return bool(get_user_roles_in_company(user, company_id=company_id) & self.allowed_roles)

            return bool(get_user_roles_in_company(user) & self.allowed_roles)

        def has_object_permission(self, request, view, obj):
            user = request.user
            if not is_user_active(user):
                return False
            if is_ceo(user):
                return True

            obj_company_ids = extract_object_company_ids(obj)
            if obj_company_ids:
                return any(
                    bool(get_user_roles_in_company(user, company_id=cid) & self.allowed_roles)
                    for cid in obj_company_ids
                )
            return bool(get_user_roles_in_company(user) & self.allowed_roles)

    _HasRolePermission.__name__ = f"HasRole_{'_'.join(sorted(normalized_roles))}"
    return _HasRolePermission


class IsCompanyMember(permissions.BasePermission):
    """
    Verifies that the active user has an active CompanyMembership for the company
    specified in the request (query/body/header) or on the target object.
    CEO users bypass company restriction; non-members receive 403.
    """

    message = "You are not an active member of this company."

    def _extract_object_company_ids(self, obj) -> set:
        return extract_object_company_ids(obj)

    def has_permission(self, request, view):
        user = request.user
        if not is_user_active(user):
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
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True

        cls_name = obj.__class__.__name__
        if cls_name in GLOBAL_READONLY_MODELS:
            return len(get_user_company_ids(user)) > 0

        # Global settings/workflow definitions (company_id=None) are readable by active company members
        if (
            cls_name in ("SystemSetting", "WorkflowStatus", "WorkflowTransition")
            and getattr(obj, "company_id", None) is None
            and request.method in permissions.SAFE_METHODS
        ):
            return len(get_user_company_ids(user)) > 0

        # Unlinked attachment uploaded by the requesting user
        if cls_name == "Attachment" and getattr(obj, "uploaded_by_id", None) == user.id:
            return True

        obj_company_ids = self._extract_object_company_ids(obj)
        if not obj_company_ids:
            return getattr(obj, "pk", None) == user.pk

        user_company_ids = get_user_company_ids(user)
        return bool(obj_company_ids & user_company_ids)


class IsCompanyMemberOrAllowAnyInDev(IsCompanyMember):
    """
    Hardened legacy compatibility alias: now strictly enforces active authentication
    and active company membership (PERM-01).
    """

    pass
