"""
reviews/permissions.py

Role-based & anti-self-approval permissions for Task Reviews (SRS §2.1–§2.6, §9, §14.2 & PERM-06):
- Reviewers must be active users holding a management role in the task's company
  (CEO, CompanyInCharge, ProjectManager, DepartmentHead, TeamLeader), the accountable
  project_manager, or the explicitly assigned task.reviewer / approval-chain approver.
- Task owners (and non-manager assignees) cannot self-review or self-approve their own tasks
  (CEO exempt as global authority).
"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied

from apps.common.permissions import (
    MANAGEMENT_ROLES,
    get_company_id_from_request,
    is_ceo,
    is_user_active,
    user_has_any_role,
)
from apps.companies.models import CompanyMembership
from apps.organization.models import DepartmentMembership, TeamMembership
from apps.projects.models import Project
from apps.rbac.models import MembershipRole
from apps.tasks.models import Task
from apps.tasks.permissions import user_is_task_manager
from apps.tasks.selectors import get_authorized_task_ids

MANAGER_OR_REVIEWER_ROLE_CODES = {
    "SUPER_ADMIN",
    "CEO",
    "COMPANYINCHARGE",
    "COMPANY_INCHARGE",
    "IN_CHARGE",
    "PROJECTMANAGER",
    "PROJECT_MANAGER",
    "DEPARTMENTHEAD",
    "DEPARTMENT_HEAD",
    "TEAMLEADER",
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
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
    ).first()
    if not membership:
        return set(), set()

    role_codes: set[str] = set()
    perm_codes: set[str] = set()
    if membership.role:
        role_codes.add(membership.role.upper())

    m_roles = MembershipRole.objects.filter(membership=membership).select_related("role").prefetch_related(
        "role__role_permissions__permission"
    )
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
    if getattr(user, "is_superuser", False) or is_ceo(user):
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
            is_active=True,
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
        raise DjangoPermissionDenied("Reviewer reassignment is disabled by company workflow policy (SRS §14.2).")
    if not is_user_authorized_reviewer(task, actor):
        raise DjangoPermissionDenied(
            f"User '{actor.full_name}' is not permitted to reassign the reviewer for this task (SRS §14.2)."
        )


def validate_can_review_task(user, task: Task) -> None:
    """
    Raises PermissionDenied (403) if `user` is not authorized to review `task`
    or is attempting to self-review their own task.
    """
    if not is_user_active(user):
        raise PermissionDenied("Your account is not active or is locked.")
    if is_ceo(user):
        return

    # Must have access to the task's authorized scope
    allowed_task_ids = get_authorized_task_ids(user) or set()
    if task.id not in allowed_task_ids:
        raise PermissionDenied("You do not have permission to review tasks in this project.")

    # Block self-review / self-approval by the task owner (SRS §9 & PERM-06)
    if task.task_owner_id == user.id:
        raise PermissionDenied("Task owners cannot review or approve their own tasks.")

    is_mgr = user_is_task_manager(user, task)
    is_assigned_reviewer = task.reviewer_id == user.id

    if not (is_mgr or is_assigned_reviewer or is_user_authorized_reviewer(task, user)):
        raise PermissionDenied(
            "Only a manager (Project Manager, Department Head, Team Leader, Company In-Charge, CEO) or the designated task reviewer can submit task reviews."
        )

    # Regular Employee assigned directly to the task cannot approve it even if misconfigured as reviewer
    if not is_mgr and task.assignments.filter(
        user=user, status__in=["Active", "Acknowledged"]
    ).exists():
        raise PermissionDenied("Assigned employees cannot review or approve their own assigned tasks.")


class CanReviewTasks(permissions.BasePermission):
    message = "You do not have permission to review or approve tasks."

    def has_permission(self, request, view):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True

        data = getattr(request, "data", None)
        if isinstance(data, dict):
            task_id = data.get("task") or data.get("task_id")
            if task_id:
                try:
                    task = Task.objects.select_related("project").filter(pk=task_id).first()
                except Exception:
                    task = None
                if task is not None:
                    validate_can_review_task(user, task)
                    return True

        company_id = get_company_id_from_request(request, view)
        if user_has_any_role(user, MANAGEMENT_ROLES, company_id=company_id):
            return True
        if Project.objects.filter(project_manager=user).exists():
            return True
        if Task.objects.filter(reviewer=user).exists():
            return True
        return False

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True
        task = getattr(obj, "task", None)
        if task is None:
            return False
        validate_can_review_task(user, task)
        if getattr(obj, "reviewer_id", None) == user.id or user_is_task_manager(user, task):
            return True
        return False
