"""
reviews/permissions.py

Role-based & anti-self-approval permissions for Task Reviews (SRS §2.1–§2.6, §9 & PERM-06):
- Reviewers must be active users holding a management role in the task's company
  (CEO, CompanyInCharge, ProjectManager, DepartmentHead, TeamLeader), the accountable
  project_manager, or the explicitly assigned task.reviewer.
- Task owners (and non-manager assignees) cannot self-review or self-approve their own tasks
  (CEO exempt as global authority).
"""
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied

from apps.common.permissions import (
    MANAGEMENT_ROLES,
    get_company_id_from_request,
    is_ceo,
    is_user_active,
    user_has_any_role,
)
from apps.projects.models import Project
from apps.tasks.models import Task
from apps.tasks.permissions import user_is_task_manager
from apps.tasks.selectors import get_authorized_task_ids


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

    if not (is_mgr or is_assigned_reviewer):
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
        # Only the original reviewer or a company/project manager may modify/delete an existing review
        if getattr(obj, "reviewer_id", None) == user.id or user_is_task_manager(user, task):
            return True
        return False
