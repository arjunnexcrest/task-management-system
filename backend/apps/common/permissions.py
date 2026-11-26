"""
common/permissions.py

Unified RBAC & Multi-Company Permission Foundation:
- Helpers:
    - get_company_id_from_request(request, view=None)
    - get_active_membership(user, company_id=None, request=None)
    - get_user_company_ids(user)
    - get_user_role_codes(user)
    - get_ordered_user_roles(user)
    - get_primary_role(user)
    - user_has_any_role(user, allowed_roles)
    - get_user_capabilities(user)
    - is_ceo(user)
- Reusable DRF Permission classes:
    - IsCEO, IsCEOOnly, HasRole(roles), IsCompanyMember, IsCompanyMemberOrAllowAnyInDev
    - RoleScopedCompanyPermission and module-specific RBAC permission classes
"""
from rest_framework import permissions

from apps.companies.models import CompanyMembership
from apps.organization.models import DepartmentMembership, TeamMembership
from apps.rbac.models import MembershipRole
from apps.rbac.roles import SystemRole


ROLE_PRECEDENCE = (
    SystemRole.CEO,
    SystemRole.COMPANY_IN_CHARGE,
    SystemRole.PROJECT_MANAGER,
    SystemRole.DEPARTMENT_HEAD,
    SystemRole.TEAM_LEADER,
    SystemRole.EMPLOYEE,
)

ROLE_CEO = {SystemRole.CEO}
ROLE_ADMIN = {SystemRole.CEO, SystemRole.COMPANY_IN_CHARGE}
ROLE_PROJECT_LEADERS = {SystemRole.CEO, SystemRole.COMPANY_IN_CHARGE, SystemRole.PROJECT_MANAGER}
ROLE_ORG_MANAGERS = {SystemRole.CEO, SystemRole.COMPANY_IN_CHARGE, SystemRole.DEPARTMENT_HEAD}
ROLE_DELIVERY_LEADS = {
    SystemRole.CEO,
    SystemRole.COMPANY_IN_CHARGE,
    SystemRole.PROJECT_MANAGER,
    SystemRole.DEPARTMENT_HEAD,
    SystemRole.TEAM_LEADER,
}

_CANONICAL_ROLE_BY_UPPER = {
    "CEO": SystemRole.CEO,
    "SUPER_ADMIN": SystemRole.CEO,
    "COMPANYINCHARGE": SystemRole.COMPANY_IN_CHARGE,
    "COMPANY_INCHARGE": SystemRole.COMPANY_IN_CHARGE,
    "IN_CHARGE": SystemRole.COMPANY_IN_CHARGE,
    "PROJECTMANAGER": SystemRole.PROJECT_MANAGER,
    "PROJECT_MANAGER": SystemRole.PROJECT_MANAGER,
    "DEPARTMENTHEAD": SystemRole.DEPARTMENT_HEAD,
    "DEPARTMENT_HEAD": SystemRole.DEPARTMENT_HEAD,
    "TEAMLEADER": SystemRole.TEAM_LEADER,
    "TEAM_LEADER": SystemRole.TEAM_LEADER,
    "EMPLOYEE": SystemRole.EMPLOYEE,
}


def _normalize_role_code(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = str(code).strip()
    if not cleaned:
        return None
    return _CANONICAL_ROLE_BY_UPPER.get(cleaned.upper(), cleaned)


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


def get_user_role_codes(user) -> set[str]:
    """
    Resolves the effective RBAC role codes for an authenticated user from:
    1. Superuser flag -> CEO
    2. Active CompanyMembership.role
    3. Active CompanyMembership -> MembershipRole -> Role.code
    4. Structural organizational assignments (Project Manager, Department Head, Team Leader)
    Falls back to {'Employee'} when no elevated role is present.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return set()

    cache_attr = "_cached_rbac_role_codes"
    cached = getattr(user, cache_attr, None)
    if cached is not None:
        return set(cached)

    roles: set[str] = set()
    if getattr(user, "is_superuser", False):
        roles.add(SystemRole.CEO)

    # Active CompanyMembership.role column
    membership_roles_col = CompanyMembership.objects.filter(
        user=user,
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
    ).values_list("role", flat=True)
    for raw_code in membership_roles_col:
        norm = _normalize_role_code(raw_code)
        if norm and norm != SystemRole.EMPLOYEE:
            roles.add(norm)

    # Explicit RBAC MembershipRole assignments
    assigned_codes = MembershipRole.objects.filter(
        membership__user=user,
        membership__is_active=True,
        membership__status=CompanyMembership.Status.ACTIVE,
    ).values_list("role__code", flat=True)
    for raw_code in assigned_codes:
        norm = _normalize_role_code(raw_code)
        if norm and norm != SystemRole.EMPLOYEE:
            roles.add(norm)

    # Structural organizational leadership roles (lazy import to avoid circularity)
    from apps.projects.models import Project

    if Project.objects.filter(project_manager=user).exists():
        roles.add(SystemRole.PROJECT_MANAGER)
    if DepartmentMembership.objects.filter(
        user=user, is_head=True, status=DepartmentMembership.Status.ACTIVE
    ).exists():
        roles.add(SystemRole.DEPARTMENT_HEAD)
    if TeamMembership.objects.filter(
        user=user, is_leader=True, status=TeamMembership.Status.ACTIVE
    ).exists():
        roles.add(SystemRole.TEAM_LEADER)

    if not roles:
        roles.add(SystemRole.EMPLOYEE)

    setattr(user, cache_attr, frozenset(roles))
    return roles


def get_ordered_user_roles(user) -> list[str]:
    codes = get_user_role_codes(user)
    ordered = [r for r in ROLE_PRECEDENCE if r in codes]
    extras = sorted(c for c in codes if c not in ROLE_PRECEDENCE)
    return ordered + extras


def get_primary_role(user) -> str:
    ordered = get_ordered_user_roles(user)
    return ordered[0] if ordered else SystemRole.EMPLOYEE


def user_has_any_role(user, allowed_roles: set[str]) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    normalized_allowed = {_normalize_role_code(r) or str(r) for r in allowed_roles}
    return bool(get_user_role_codes(user) & normalized_allowed)


def is_ceo(user) -> bool:
    """Return True if the authenticated user is a superuser or holds an active CEO role."""
    if not user or not getattr(user, "is_authenticated", False):
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
        return MembershipRole.objects.filter(
            membership__user=user,
            membership__is_active=True,
            membership__status=CompanyMembership.Status.ACTIVE,
            role__code__iexact=SystemRole.CEO,
        ).exists()
    except Exception:
        return False


def get_user_capabilities(user) -> dict[str, bool]:
    """
    Single source of truth for UI and API capabilities per role.
    """
    roles = get_user_role_codes(user)
    ceo = SystemRole.CEO in roles or getattr(user, "is_superuser", False)
    admin = ceo or SystemRole.COMPANY_IN_CHARGE in roles
    pm = admin or SystemRole.PROJECT_MANAGER in roles
    dept_head = admin or SystemRole.DEPARTMENT_HEAD in roles
    lead = pm or dept_head or SystemRole.TEAM_LEADER in roles

    return {
        "can_view_ceo_dashboard": ceo,
        "can_manage_companies": ceo,
        "can_view_companies": admin,
        "can_manage_rbac": ceo,
        "can_manage_settings": ceo,
        "can_manage_users": admin,
        "can_view_employees": lead,
        "can_create_departments": admin,
        "can_manage_departments": dept_head,
        "can_view_departments": lead,
        "can_create_teams": dept_head,
        "can_manage_teams": dept_head,
        "can_manage_team_members": dept_head or SystemRole.TEAM_LEADER in roles,
        "can_view_teams": lead,
        "can_create_projects": pm,
        "can_edit_projects": pm,
        "can_delete_projects": admin,
        "can_manage_project_assignments": pm or dept_head,
        "can_create_tasks": lead,
        "can_edit_tasks": lead,
        "can_delete_tasks": pm or dept_head,
        "can_assign_tasks": lead,
        "can_review_tasks": lead,
        "can_configure_approval_chains": pm,
        "can_view_workload": lead,
        "can_view_reports": lead,
        "can_manage_risks": lead,
    }


class IsCEO(permissions.BasePermission):
    """Allow access only to users with the CEO role (or superuser)."""

    message = "Only the CEO can perform this action."

    def has_permission(self, request, view):
        return is_ceo(request.user)


class IsCEOOnly(permissions.BasePermission):
    """Strictly requires an authenticated CEO / Superuser."""

    message = "Access denied: Only the CEO can access this executive resource."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and is_ceo(request.user)
        )


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
                if membership.role.upper() in self.allowed_roles:
                    return True
                return MembershipRole.objects.filter(
                    membership=membership,
                    role__code__in=[r for r in roles],
                ).exists()

            user_roles = CompanyMembership.objects.filter(
                user=user,
                is_active=True,
                status=CompanyMembership.Status.ACTIVE,
            ).values_list("role", flat=True)
            if any(r.upper() in self.allowed_roles for r in user_roles):
                return True
            return any(r.upper() in self.allowed_roles for r in get_user_role_codes(user))

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
    Legacy compatibility permission used by projects, tasks, reviews, tracking, notifications, etc.
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
        if company_id is None and hasattr(obj, "department") and obj.department is not None:
            company_id = obj.department.company_id
        if company_id is None and hasattr(obj, "team") and obj.team is not None:
            company_id = obj.team.department.company_id

        if company_id is None:
            return True

        return get_active_membership(request.user, company_id=company_id) is not None


class RoleScopedCompanyPermission(IsCompanyMemberOrAllowAnyInDev):
    """
    Enforces role-based write/delete/read permissions for authenticated users
    while preserving dev/unauthenticated test compatibility when anonymous.
    """

    write_roles: set[str] = ROLE_CEO
    delete_roles: set[str] | None = None
    read_roles: set[str] | None = None
    action_roles: dict[str, set[str]] = {}
    message = "You do not have permission to perform this action with your current role."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return True

        action = getattr(view, "action", None)
        if action and action in self.action_roles:
            allowed = self.action_roles[action]
            if not user_has_any_role(request.user, allowed):
                return False
            return True

        if request.method in permissions.SAFE_METHODS:
            if self.read_roles is not None:
                return user_has_any_role(request.user, self.read_roles)
            return True

        if request.method == "DELETE" and self.delete_roles is not None:
            return user_has_any_role(request.user, self.delete_roles)

        return user_has_any_role(request.user, self.write_roles)


class CompanyManagementPermission(RoleScopedCompanyPermission):
    """Only CEO can create, edit, or deactivate companies."""

    write_roles = ROLE_CEO
    delete_roles = ROLE_CEO
    message = "Only the CEO can create, update, or deactivate companies."


class RBACAndSettingsPermission(RoleScopedCompanyPermission):
    """Only CEO can create, modify, or delete RBAC roles, permissions, and system settings."""

    write_roles = ROLE_CEO
    delete_roles = ROLE_CEO
    message = "Only the CEO can modify roles, permissions, or system settings."


class UserManagementPermission(RoleScopedCompanyPermission):
    """Only CEO and Company In-Charge can create, edit, activate, deactivate, suspend, or delete users."""

    write_roles = ROLE_ADMIN
    delete_roles = ROLE_CEO
    action_roles = {
        "activate": ROLE_ADMIN,
        "deactivate": ROLE_ADMIN,
        "suspend": ROLE_ADMIN,
    }
    message = "Only CEO or Company In-Charge can create or manage user accounts."


class DepartmentManagementPermission(RoleScopedCompanyPermission):
    """
    CEO and Company In-Charge have full CRUD on departments.
    Department Head may transfer employees within their department.
    Project Manager, Team Leader, and Employee are read-only.
    """

    write_roles = ROLE_ADMIN
    delete_roles = ROLE_ADMIN
    action_roles = {
        "assign_head": ROLE_ADMIN,
        "transfer_employee": ROLE_ORG_MANAGERS,
    }
    message = "Only CEO or Company In-Charge can create, edit, or deactivate departments."


class DepartmentMembershipPermission(RoleScopedCompanyPermission):
    write_roles = ROLE_ORG_MANAGERS
    delete_roles = ROLE_ADMIN
    message = "Only CEO, Company In-Charge, or Department Head can manage department memberships."


class TeamManagementPermission(RoleScopedCompanyPermission):
    """
    CEO, Company In-Charge, and Department Head can create, edit, deactivate teams and assign leaders.
    Team Leaders, Project Managers, and Employees cannot create/edit/deactivate teams.
    """

    write_roles = ROLE_ORG_MANAGERS
    delete_roles = ROLE_ADMIN
    action_roles = {
        "assign_leader": ROLE_ORG_MANAGERS,
    }
    message = "Only CEO, Company In-Charge, or Department Head can manage teams."


class TeamMembershipPermission(RoleScopedCompanyPermission):
    """CEO, Company In-Charge, Department Head, and Team Leader can manage team rosters."""

    write_roles = ROLE_ORG_MANAGERS | {SystemRole.TEAM_LEADER}
    delete_roles = ROLE_ORG_MANAGERS | {SystemRole.TEAM_LEADER}
    message = "Only CEO, Company In-Charge, Department Head, or Team Leader can manage team members."


class ProjectManagementPermission(RoleScopedCompanyPermission):
    """
    - CEO / CompanyInCharge: full CRUD + delete + assign PM + close + archive
    - ProjectManager: create, edit, close, archive, map departments/teams, assign members (no hard delete)
    - DepartmentHead: map departments/teams, sync team members, assign members (read-only on project CRUD)
    - TeamLeader / Employee: read-only on projects
    """

    write_roles = ROLE_PROJECT_LEADERS
    delete_roles = ROLE_ADMIN
    action_roles = {
        "assign_manager": ROLE_ADMIN,
        "map_departments": ROLE_PROJECT_LEADERS | {SystemRole.DEPARTMENT_HEAD},
        "map_teams": ROLE_PROJECT_LEADERS | {SystemRole.DEPARTMENT_HEAD},
        "sync_team_members": ROLE_PROJECT_LEADERS | {SystemRole.DEPARTMENT_HEAD},
        "assign_members": ROLE_PROJECT_LEADERS | {SystemRole.DEPARTMENT_HEAD},
        "close_project": ROLE_PROJECT_LEADERS,
        "archive": ROLE_PROJECT_LEADERS,
        "recalculate_progress": ROLE_DELIVERY_LEADS,
    }
    message = "You do not have permission to create, modify, or delete projects."


class ProjectParticipationWritePermission(RoleScopedCompanyPermission):
    """For ProjectMember, ProjectDepartment, ProjectTeam ViewSets."""

    write_roles = ROLE_PROJECT_LEADERS | {SystemRole.DEPARTMENT_HEAD}
    delete_roles = ROLE_PROJECT_LEADERS | {SystemRole.DEPARTMENT_HEAD}
    message = "Only CEO, Company In-Charge, Project Manager, or Department Head can modify project participation."


class ProjectPlanningWritePermission(RoleScopedCompanyPermission):
    """For ProjectPhase, ProjectMilestone, MilestoneTask, MilestoneDependency ViewSets."""

    write_roles = ROLE_PROJECT_LEADERS
    delete_roles = ROLE_PROJECT_LEADERS
    message = "Only CEO, Company In-Charge, or Project Manager can modify project phases and milestones."


class TaskManagementPermission(RoleScopedCompanyPermission):
    """
    - Delivery leads (CEO, CompanyInCharge, PM, DeptHead, TeamLeader): create, edit, assign, bulk-assign, reschedule
    - Delete task: CEO, CompanyInCharge, PM, DeptHead
    - Employees: read tasks, transition status, submit for review, post comments, log progress updates,
      log time entries, and upload task attachments (no create/edit/delete/assign/review decisions/chain config).
    """

    write_roles = ROLE_DELIVERY_LEADS
    delete_roles = ROLE_PROJECT_LEADERS | {SystemRole.DEPARTMENT_HEAD}
    action_roles = {
        "transition_status": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "submit_for_review": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "comments": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "progress_updates": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "time_entries": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "attachments": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "assign": ROLE_DELIVERY_LEADS,
        "bulk_assign": ROLE_DELIVERY_LEADS,
        "reschedule": ROLE_DELIVERY_LEADS,
        "check_deadlines": ROLE_DELIVERY_LEADS,
        "reassign_reviewer": ROLE_DELIVERY_LEADS,
        "reviews": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "review_chain": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "working_calendar": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
    }
    message = "Employees cannot create, edit, delete, or reassign tasks. Contact your Team Leader or Project Manager."

    def has_permission(self, request, view):
        action = getattr(view, "action", None)
        if request.user and request.user.is_authenticated and request.method not in permissions.SAFE_METHODS:
            if action == "working_calendar":
                return user_has_any_role(request.user, ROLE_PROJECT_LEADERS)
            if action == "reviews":
                return user_has_any_role(request.user, ROLE_DELIVERY_LEADS)
            if action == "review_chain":
                return user_has_any_role(request.user, ROLE_PROJECT_LEADERS)
        return super().has_permission(request, view)


class TaskAssignmentPermission(RoleScopedCompanyPermission):
    """
    - Employees may acknowledge their own assignments (`acknowledge`).
    - Creating, updating, ending, or deleting task assignments requires a lead/manager role.
    """

    write_roles = ROLE_DELIVERY_LEADS
    delete_roles = ROLE_DELIVERY_LEADS
    action_roles = {
        "acknowledge": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "end": ROLE_DELIVERY_LEADS,
    }
    message = "Only Team Leaders, Department Heads, Project Managers, or Executives can assign or reassign tasks."


class TaskDependencyPermission(RoleScopedCompanyPermission):
    """Creating or deleting task dependencies requires a delivery lead role."""

    write_roles = ROLE_DELIVERY_LEADS
    delete_roles = ROLE_DELIVERY_LEADS
    message = "Only Team Leaders, Department Heads, Project Managers, or Executives can modify task dependencies."


class ReviewManagementPermission(RoleScopedCompanyPermission):
    """
    - All authenticated members (including Employees) may submit tasks for review (`submit`).
    - Approving, rejecting, requesting changes, reassigning reviewers, or escalating SLAs requires a reviewer/lead role.
    - Configuring approval chain rules requires CEO, CompanyInCharge, or ProjectManager.
    """

    write_roles = ROLE_DELIVERY_LEADS
    delete_roles = ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE}
    action_roles = {
        "update": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "partial_update": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "submit": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
        "submit_decision": ROLE_DELIVERY_LEADS,
        "reassign_reviewer": ROLE_DELIVERY_LEADS,
        "escalate_overdue": ROLE_DELIVERY_LEADS,
        "approval_chain": ROLE_DELIVERY_LEADS | {SystemRole.EMPLOYEE},
    }
    message = "Only Team Leaders, Department Heads, Project Managers, or Executives can review or approve tasks."

    def has_permission(self, request, view):
        if (
            getattr(view, "action", None) == "approval_chain"
            and request.method not in permissions.SAFE_METHODS
            and request.user
            and request.user.is_authenticated
        ):
            return user_has_any_role(request.user, ROLE_PROJECT_LEADERS)
        return super().has_permission(request, view)


class ReportAccessPermission(RoleScopedCompanyPermission):
    """Organization/project/team reports and CSV exports are restricted to leadership & management roles."""

    read_roles = ROLE_DELIVERY_LEADS
    write_roles = ROLE_DELIVERY_LEADS
    message = "Reports and exports are available only to Team Leaders, Department Heads, Project Managers, and Executives."


class RiskManagementPermission(RoleScopedCompanyPermission):
    """Risks and Issues CRUD restricted to delivery leads (Blockers remain reportable by Employees)."""

    write_roles = ROLE_DELIVERY_LEADS
    delete_roles = ROLE_PROJECT_LEADERS
    message = "Only Team Leaders, Department Heads, Project Managers, or Executives can modify risks and issues."
