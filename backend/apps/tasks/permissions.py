"""
tasks/permissions.py

Role-based & multi-company permissions for Task management operations (SRS §2.1–§2.6, §7 & PERM-05):
- Management roles (CEO, CompanyInCharge, ProjectManager, DepartmentHead, TeamLeader) or the
  accountable project.project_manager can delete tasks, assign/reassign/bulk-assign tasks,
  reschedule tasks, and manage task dependencies.
- Regular Employees can execute assigned tasks (update status/progress, create subtasks,
  toggle checklists, post comments, upload attachments, log time, acknowledge assignments),
  but receive 403 Forbidden on destructive or managerial actions.
"""
from rest_framework import permissions

from apps.common.permissions import (
    MANAGEMENT_ROLES,
    get_company_id_from_request,
    is_ceo,
    is_user_active,
    user_has_any_role,
)
from apps.projects.models import Project
from apps.tasks.models import Task


def user_is_project_manager_or_management(user, project: Project | None) -> bool:
    """Return True if active user is CEO, holds a management role in project.company, or is project.project_manager."""
    if not is_user_active(user):
        return False
    if is_ceo(user):
        return True
    if project is None:
        return False
    if project.project_manager_id == user.id:
        return True
    return user_has_any_role(user, MANAGEMENT_ROLES, company_id=project.company_id)


def user_is_task_manager(user, task: Task | None) -> bool:
    if task is None:
        return False
    return user_is_project_manager_or_management(user, getattr(task, "project", None))


def _resolve_project_from_obj(obj):
    if obj is None:
        return None
    if isinstance(obj, Project):
        return obj
    if isinstance(obj, Task):
        return obj.project
    proj = getattr(obj, "project", None)
    if proj is not None:
        return proj
    task = getattr(obj, "task", None)
    if task is not None:
        return getattr(task, "project", None)
    checklist = getattr(obj, "checklist", None)
    if checklist is not None and getattr(checklist, "task", None) is not None:
        return checklist.task.project
    comment = getattr(obj, "comment", None)
    if comment is not None and getattr(comment, "task", None) is not None:
        return comment.task.project
    progress_update = getattr(obj, "progress_update", None)
    if progress_update is not None and getattr(progress_update, "task", None) is not None:
        return progress_update.task.project
    return None


def _resolve_project_from_request(request):
    data = getattr(request, "data", None)
    if not isinstance(data, dict):
        return None
    task_id = data.get("task") or data.get("task_id")
    if task_id:
        try:
            t = Task.objects.select_related("project").filter(pk=task_id).first()
            if t is not None:
                return t.project
        except Exception:
            pass
    project_id = data.get("project") or data.get("project_id")
    if project_id:
        try:
            return Project.objects.filter(pk=project_id).first()
        except Exception:
            pass
    return None


class CanManageTasks(permissions.BasePermission):
    """
    Allows CEO, CompanyInCharge, ProjectManager, DepartmentHead, TeamLeader (within that company)
    or the accountable project_manager of the target project. Regular Employees get 403.
    """

    message = "Only managers (CEO, Company In-Charge, Project Manager, Department Head, Team Leader) can perform this task management action."

    def has_permission(self, request, view):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True

        target_project = _resolve_project_from_request(request)
        if target_project is not None:
            return user_is_project_manager_or_management(user, target_project)

        company_id = get_company_id_from_request(request, view)
        if user_has_any_role(user, MANAGEMENT_ROLES, company_id=company_id):
            return True

        # Accountable Project Manager of at least one project (object-level check verifies exact project)
        if Project.objects.filter(project_manager=user).exists():
            return True
        return False

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True
        if obj.__class__.__name__ == "TaskTag":
            return user_has_any_role(user, MANAGEMENT_ROLES)
        project = _resolve_project_from_obj(obj)
        return user_is_project_manager_or_management(user, project)
