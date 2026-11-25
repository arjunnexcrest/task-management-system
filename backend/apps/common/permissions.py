from rest_framework import permissions

from apps.companies.models import CompanyMembership
from apps.organization.models import DepartmentMembership, TeamMembership
from apps.rbac.models import MembershipRole


ROLE_PRECEDENCE = (
    "CEO",
    "CompanyInCharge",
    "ProjectManager",
    "DepartmentHead",
    "TeamLeader",
    "Employee",
)

ROLE_CEO = {"CEO"}
ROLE_ADMIN = {"CEO", "CompanyInCharge"}
ROLE_PROJECT_LEADERS = {"CEO", "CompanyInCharge", "ProjectManager"}
ROLE_ORG_MANAGERS = {"CEO", "CompanyInCharge", "DepartmentHead"}
ROLE_DELIVERY_LEADS = {
    "CEO",
    "CompanyInCharge",
    "ProjectManager",
    "DepartmentHead",
    "TeamLeader",
}


def get_user_role_codes(user) -> set[str]:
    """
    Resolves the effective RBAC role codes for an authenticated user from:
    1. Superuser flag -> CEO
    2. Active CompanyMembership -> MembershipRole -> Role.code
    3. Structural organizational assignments (Project Manager, Department Head, Team Leader)
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
        roles.add("CEO")

    # Explicit RBAC role assignments
    assigned_codes = MembershipRole.objects.filter(
        membership__user=user,
        membership__status=CompanyMembership.Status.ACTIVE,
    ).values_list("role__code", flat=True)
    for code in assigned_codes:
        if code:
            roles.add(code)

    # Structural organizational leadership roles (lazy import to avoid circularity)
    from apps.projects.models import Project

    if Project.objects.filter(project_manager=user).exists():
        roles.add("ProjectManager")
    if DepartmentMembership.objects.filter(
        user=user, is_head=True, status=DepartmentMembership.Status.ACTIVE
    ).exists():
        roles.add("DepartmentHead")
    if TeamMembership.objects.filter(
        user=user, is_leader=True, status=TeamMembership.Status.ACTIVE
    ).exists():
        roles.add("TeamLeader")

    if not roles:
        roles.add("Employee")

    setattr(user, cache_attr, frozenset(roles))
    return roles


def get_ordered_user_roles(user) -> list[str]:
    codes = get_user_role_codes(user)
    ordered = [r for r in ROLE_PRECEDENCE if r in codes]
    extras = sorted(c for c in codes if c not in ROLE_PRECEDENCE)
    return ordered + extras


def get_primary_role(user) -> str:
    ordered = get_ordered_user_roles(user)
    return ordered[0] if ordered else "Employee"


def user_has_any_role(user, allowed_roles: set[str]) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(get_user_role_codes(user) & allowed_roles)


def is_ceo(user) -> bool:
    return user_has_any_role(user, ROLE_CEO)


def get_user_capabilities(user) -> dict[str, bool]:
    """
    Single source of truth for UI and API capabilities per role.
    """
    roles = get_user_role_codes(user)
    ceo = "CEO" in roles or getattr(user, "is_superuser", False)
    admin = ceo or "CompanyInCharge" in roles
    pm = admin or "ProjectManager" in roles
    dept_head = admin or "DepartmentHead" in roles
    lead = pm or dept_head or "TeamLeader" in roles

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
        "can_manage_team_members": dept_head or "TeamLeader" in roles,
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

        return CompanyMembership.objects.filter(
            company_id=company_id,
            user=request.user,
            status=CompanyMembership.Status.ACTIVE,
        ).exists()


class IsCEOOnly(permissions.BasePermission):
    """Strictly requires an authenticated CEO / Superuser."""

    message = "Access denied: Only the CEO can access this executive resource."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and is_ceo(request.user)
        )


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

    write_roles = ROLE_ORG_MANAGERS | {"TeamLeader"}
    delete_roles = ROLE_ORG_MANAGERS | {"TeamLeader"}
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
        "map_departments": ROLE_PROJECT_LEADERS | {"DepartmentHead"},
        "map_teams": ROLE_PROJECT_LEADERS | {"DepartmentHead"},
        "sync_team_members": ROLE_PROJECT_LEADERS | {"DepartmentHead"},
        "assign_members": ROLE_PROJECT_LEADERS | {"DepartmentHead"},
        "close_project": ROLE_PROJECT_LEADERS,
        "archive": ROLE_PROJECT_LEADERS,
        "recalculate_progress": ROLE_DELIVERY_LEADS,
    }
    message = "You do not have permission to create, modify, or delete projects."


class ProjectParticipationWritePermission(RoleScopedCompanyPermission):
    """For ProjectMember, ProjectDepartment, ProjectTeam ViewSets."""

    write_roles = ROLE_PROJECT_LEADERS | {"DepartmentHead"}
    delete_roles = ROLE_PROJECT_LEADERS | {"DepartmentHead"}
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
    delete_roles = ROLE_PROJECT_LEADERS | {"DepartmentHead"}
    action_roles = {
        "transition_status": ROLE_DELIVERY_LEADS | {"Employee"},
        "submit_for_review": ROLE_DELIVERY_LEADS | {"Employee"},
        "comments": ROLE_DELIVERY_LEADS | {"Employee"},
        "progress_updates": ROLE_DELIVERY_LEADS | {"Employee"},
        "time_entries": ROLE_DELIVERY_LEADS | {"Employee"},
        "attachments": ROLE_DELIVERY_LEADS | {"Employee"},
        "assign": ROLE_DELIVERY_LEADS,
        "bulk_assign": ROLE_DELIVERY_LEADS,
        "reschedule": ROLE_DELIVERY_LEADS,
        "check_deadlines": ROLE_DELIVERY_LEADS,
        "reassign_reviewer": ROLE_DELIVERY_LEADS,
        "reviews": ROLE_DELIVERY_LEADS | {"Employee"},
        "review_chain": ROLE_DELIVERY_LEADS | {"Employee"},
        "working_calendar": ROLE_DELIVERY_LEADS | {"Employee"},
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
        "acknowledge": ROLE_DELIVERY_LEADS | {"Employee"},
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
    delete_roles = ROLE_DELIVERY_LEADS | {"Employee"}
    action_roles = {
        "submit": ROLE_DELIVERY_LEADS | {"Employee"},
        "submit_decision": ROLE_DELIVERY_LEADS,
        "reassign_reviewer": ROLE_DELIVERY_LEADS,
        "escalate_overdue": ROLE_DELIVERY_LEADS,
        "approval_chain": ROLE_DELIVERY_LEADS | {"Employee"},
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
