"""
projects/permissions.py

Role-based & multi-company permissions for Project CRUD, Planning, and Assignment (SRS §2.1–§2.6, §6.3 & PERM-04):
- CEO: full control across all companies.
- Company In-Charge: full project control within their active company (including assign_manager & delete).
- Project Manager: create projects in their company, manage their assigned projects (update, phases,
  milestones, departments, teams, members, close, archive, recalculate_progress); cannot assign_manager
  or hard-delete projects.
- Department Head: map teams, sync team members, and manage ProjectTeam / ProjectMember records on
  projects where their department participates (SRS §2.4 & §6.3 Step 6).
- Team Leader & Employee: read-only on assigned projects; 403 Forbidden on project mutations.
"""
from rest_framework import permissions

from apps.common.permissions import (
    ProjectManagementPermission,
    ProjectParticipationWritePermission,
    ProjectPlanningWritePermission,
    get_active_membership,
    get_company_id_from_request,
    get_user_roles_in_company,
    is_ceo,
    is_user_active,
    user_has_any_role,
)
from apps.rbac.roles import SystemRole
from .models import Project, ProjectDepartment, ProjectMilestone

COMPANY_ADMIN_ROLES = {
    SystemRole.CEO.upper(),
    SystemRole.COMPANY_IN_CHARGE.upper(),
}

PROJECT_CREATOR_ROLES = {
    SystemRole.CEO.upper(),
    SystemRole.COMPANY_IN_CHARGE.upper(),
    SystemRole.PROJECT_MANAGER.upper(),
}

DEPT_HEAD_ALLOWED_PROJECT_ACTIONS = {
    "map_teams",
    "sync_team_members",
    "assign_members",
}

DEPT_HEAD_ALLOWED_VIEWSETS = {
    "ProjectTeamViewSet",
    "ProjectMemberViewSet",
}


def _resolve_project_from_obj(obj):
    if obj is None:
        return None
    if isinstance(obj, Project):
        return obj
    proj = getattr(obj, "project", None)
    if proj is not None:
        return proj
    milestone = getattr(obj, "milestone", None)
    if milestone is not None:
        return getattr(milestone, "project", None)
    return None


def _resolve_project_from_request(request):
    data = getattr(request, "data", None)
    if not isinstance(data, dict):
        return None
    project_id = data.get("project") or data.get("project_id")
    if project_id:
        try:
            return Project.objects.select_related("company").filter(pk=project_id).first()
        except Exception:
            return None
    milestone_id = data.get("milestone") or data.get("milestone_id")
    if milestone_id:
        try:
            ms = ProjectMilestone.objects.select_related("project", "project__company").filter(
                pk=milestone_id
            ).first()
            return ms.project if ms else None
        except Exception:
            return None
    return None


def _is_dept_head_for_project(user, project: Project) -> bool:
    from apps.organization.models import DepartmentMembership

    mapped_dept_ids = list(
        ProjectDepartment.objects.filter(project=project).values_list("department_id", flat=True)
    )
    if not mapped_dept_ids:
        return False
    return DepartmentMembership.objects.filter(
        user=user,
        department_id__in=mapped_dept_ids,
        status=DepartmentMembership.Status.ACTIVE,
        is_head=True,
    ).exists()


class CanManageProjects(permissions.BasePermission):
    message = "You do not have permission to modify this project or its assignments."

    def _is_hard_delete_project(self, request, view) -> bool:
        return (
            view.__class__.__name__ == "ProjectViewSet"
            and getattr(view, "action", None) == "destroy"
            and request.query_params.get("mode", "").lower() != "archive"
        )

    def _is_assign_manager(self, view) -> bool:
        return (
            view.__class__.__name__ == "ProjectViewSet"
            and getattr(view, "action", None) == "assign_manager"
        )

    def _allows_dept_head(self, view) -> bool:
        view_name = view.__class__.__name__
        if view_name in DEPT_HEAD_ALLOWED_VIEWSETS:
            return True
        if view_name == "ProjectViewSet" and getattr(view, "action", None) in DEPT_HEAD_ALLOWED_PROJECT_ACTIONS:
            return True
        return False

    def _can_act_on_project(self, request, view, project: Project) -> bool:
        user = request.user
        if is_ceo(user):
            return True
        if project is None:
            return False

        membership = get_active_membership(user, company_id=project.company_id)
        if not membership:
            return False

        roles = get_user_roles_in_company(user, company_id=project.company_id)

        # CEO & Company In-Charge have full project authority within their company
        if roles & COMPANY_ADMIN_ROLES:
            return True

        # Only CEO & Company In-Charge can assign/reassign Project Manager or hard-delete a project
        if self._is_assign_manager(view) or self._is_hard_delete_project(request, view):
            return False

        # Project Manager can manage projects they are accountable for (or unassigned Drafts in their company)
        if project.project_manager_id == user.id or (
            SystemRole.PROJECT_MANAGER.upper() in roles and project.project_manager_id is None
        ):
            return True

        # Department Head can map teams & assign members on projects mapped to their department
        if self._allows_dept_head(view) and _is_dept_head_for_project(user, project):
            return True

        return False

    def has_permission(self, request, view):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True

        # assign_manager and hard DELETE on ProjectViewSet: CEO or Company In-Charge only
        if self._is_assign_manager(view) or self._is_hard_delete_project(request, view):
            company_id = get_company_id_from_request(request, view)
            return user_has_any_role(user, COMPANY_ADMIN_ROLES, company_id=company_id)

        # Creating a Project on ProjectViewSet: CEO, CompanyInCharge, or ProjectManager of that company
        if view.__class__.__name__ == "ProjectViewSet" and getattr(view, "action", None) == "create":
            company_id = get_company_id_from_request(request, view)
            if company_id:
                return user_has_any_role(user, PROJECT_CREATOR_ROLES, company_id=company_id)
            return user_has_any_role(user, PROJECT_CREATOR_ROLES)

        # Creating a child object (ProjectMember, ProjectPhase, ProjectTeam, etc.): validate target project
        if request.method == "POST" and getattr(view, "action", None) == "create":
            target_project = _resolve_project_from_request(request)
            if target_project is not None:
                return self._can_act_on_project(request, view, target_project)

        # For detail actions, check user holds at least one qualifying role or is a PM/DeptHead
        company_id = get_company_id_from_request(request, view)
        if user_has_any_role(user, PROJECT_CREATOR_ROLES, company_id=company_id):
            return True

        if Project.objects.filter(project_manager=user).exists():
            return True

        if self._allows_dept_head(view):
            from apps.organization.models import DepartmentMembership

            if user_has_any_role(user, [SystemRole.DEPARTMENT_HEAD], company_id=company_id):
                return True
            if DepartmentMembership.objects.filter(
                user=user, status=DepartmentMembership.Status.ACTIVE, is_head=True
            ).exists():
                return True

        return False

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True
        project = _resolve_project_from_obj(obj)
        return self._can_act_on_project(request, view, project)


__all__ = [
    "CanManageProjects",
    "ProjectManagementPermission",
    "ProjectParticipationWritePermission",
    "ProjectPlanningWritePermission",
]
