from django.core.exceptions import PermissionDenied
from apps.companies.models import CompanyMembership
from apps.organization.models import DepartmentMembership, TeamMembership
from apps.rbac.models import MembershipRole


MANAGER_OR_REVIEWER_ROLE_CODES = {
    "SUPER_ADMIN",
    "CEO",
    "COMPANY_INCHARGE",
    "IN_CHARGE",
    "PROJECT_MANAGER",
    "DEPARTMENT_HEAD",
    "TEAM_LEADER",
    "REVIEWER",
    "QA_LEAD",
}

REVIEW_PERMISSION_CODES = {
    "task.review",
    "task.approve",
    "review.approve",
    "review.create",
    "task.assign",
}


def get_user_company_roles_and_perms(user, company) -> tuple[set[str], set[str]]:
    if not user or not company:
        return set(), set()
    membership = CompanyMembership.objects.filter(
        company=company,
        user=user,
        status=CompanyMembership.Status.ACTIVE,
    ).first()
    if not membership:
        return set(), set()

    m_roles = MembershipRole.objects.filter(membership=membership).select_related("role").prefetch_related(
        "role__role_permissions__permission"
    )
    role_codes: set[str] = set()
    perm_codes: set[str] = set()
    for mr in m_roles:
        if mr.role and mr.role.code:
            role_codes.add(mr.role.code.upper())
            for rp in mr.role.role_permissions.all():
                if rp.permission and rp.permission.code:
                    perm_codes.add(rp.permission.code.lower())
    return role_codes, perm_codes


def is_user_authorized_reviewer(task, user, extra_chain_user_ids: set[str] | None = None) -> bool:
    """
    SRS §2, §14.2 & §27.5:
    Determines whether `user` is permitted to review, approve, reject, or request corrections
    for `task` within its company boundary.
    """
    if not user:
        return False
    if not getattr(user, "is_active", True) or getattr(user, "account_status", "Active") != "Active":
        return False

    company = task.project.company
    if getattr(user, "is_superuser", False):
        return True

    # Ensure active company membership when memberships are configured
    has_memberships = (
        CompanyMembership.objects.filter(company=company).exists()
        or CompanyMembership.objects.filter(user=user).exists()
    )
    if has_memberships:
        is_member = CompanyMembership.objects.filter(
            company=company,
            user=user,
            status=CompanyMembership.Status.ACTIVE,
        ).exists()
        if not is_member:
            return False

    role_codes, perm_codes = get_user_company_roles_and_perms(user, company)

    # Pure Viewer / External Collaborator role cannot approve/reject unless explicitly elevated
    if role_codes and role_codes.issubset({"VIEWER", "EXTERNAL_COLLABORATOR"}) and not (perm_codes & REVIEW_PERMISSION_CODES):
        return False

    # Direct task reviewer, Project Manager, or explicit chain step approver
    if task.reviewer_id == user.id:
        return True
    if task.project.project_manager_id == user.id:
        return True
    if extra_chain_user_ids and str(user.id) in extra_chain_user_ids:
        return True

    # Department Head of the task's department
    dept_id = task.department_id or (task.team.department_id if task.team_id else None)
    if dept_id and DepartmentMembership.objects.filter(
        department_id=dept_id,
        user=user,
        is_head=True,
        status=DepartmentMembership.Status.ACTIVE,
    ).exists():
        return True

    # Team Leader of the task's team
    if task.team_id and TeamMembership.objects.filter(
        team_id=task.team_id,
        user=user,
        is_leader=True,
        status=TeamMembership.Status.ACTIVE,
    ).exists():
        return True

    # RBAC role or permission
    if (role_codes & MANAGER_OR_REVIEWER_ROLE_CODES) or (perm_codes & REVIEW_PERMISSION_CODES):
        return True

    return False


def assert_can_reassign_reviewer(task, actor, allow_reassignment_policy: bool = True) -> None:
    """
    SRS §14.2: "Reassign a reviewer when permitted."
    """
    if not allow_reassignment_policy and not getattr(actor, "is_superuser", False):
        raise PermissionDenied("Reviewer reassignment is disabled by company workflow policy (SRS §14.2).")
    if not is_user_authorized_reviewer(task, actor):
        raise PermissionDenied(
            f"User '{actor.full_name}' is not permitted to reassign the reviewer for this task (SRS §14.2)."
        )
