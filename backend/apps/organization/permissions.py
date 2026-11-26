"""
organization/permissions.py

Role-based and multi-company permissions for Department management (SRS §5.1):
- CEO has full access across all companies.
- Company In-Charge can manage departments and assign department heads within their own company.
- Other active company members can view departments in their own company.
- Non-members receive 403 Forbidden.
"""
from rest_framework import permissions

from apps.common.permissions import (
    get_active_membership,
    get_company_id_from_request,
    is_ceo,
)
from apps.companies.models import CompanyMembership
from apps.rbac.roles import SystemRole

DEPARTMENT_MANAGER_ROLES = {
    SystemRole.CEO.upper(),
    SystemRole.COMPANY_IN_CHARGE.upper(),
}


class CanManageDepartments(permissions.BasePermission):
    """
    Allows CEO (globally) or Company In-Charge (scoped to their active company)
    to create, update, deactivate/activate departments, assign department heads,
    and transfer employees between departments.
    """

    message = "Only the CEO or Company In-Charge can manage departments and assign department heads."

    def _extract_company_id(self, obj):
        if obj is None:
            return None
        cid = getattr(obj, "company_id", None)
        if cid:
            return cid
        department = getattr(obj, "department", None)
        if department is not None:
            return department.company_id
        return None

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True

        company_id = get_company_id_from_request(request, view)
        if company_id:
            membership = get_active_membership(user, company_id=company_id)
            if not membership:
                return False
            return membership.role.upper() in DEPARTMENT_MANAGER_ROLES

        # For detail actions where company_id comes from the target object,
        # confirm the user holds a manager role in at least one active company;
        # has_object_permission then verifies the exact company match.
        user_roles = CompanyMembership.objects.filter(
            user=user,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
        ).values_list("role", flat=True)
        return any(r.upper() in DEPARTMENT_MANAGER_ROLES for r in user_roles)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True

        company_id = self._extract_company_id(obj)
        if not company_id:
            return False

        membership = get_active_membership(user, company_id=company_id)
        if not membership:
            return False
        return membership.role.upper() in DEPARTMENT_MANAGER_ROLES


TEAM_MANAGER_ROLES = {
    SystemRole.CEO.upper(),
    SystemRole.COMPANY_IN_CHARGE.upper(),
    SystemRole.DEPARTMENT_HEAD.upper(),
}


def _extract_department_id_from_request(request, view=None):
    if request is None:
        return None
    dept_id = request.query_params.get("department") or request.query_params.get("department_id")
    if not dept_id and hasattr(request, "data") and isinstance(request.data, dict):
        dept_id = request.data.get("department") or request.data.get("department_id")
    if not dept_id and view is not None:
        dept_id = view.kwargs.get("department_id") or view.kwargs.get("department_pk")
    return dept_id or None


class CanManageTeams(permissions.BasePermission):
    """
    Team CRUD & Team-Leader assignment permission (SRS §5.2):
    - CEO: full access across all companies.
    - Company In-Charge: full team management within their active company.
    - Department Head: can manage teams under their own department (or their company
      if they hold the DepartmentHead role without being pinned to a different department).
    - Employees / Team Leaders / non-members receive 403 Forbidden.
    """

    message = "Only the CEO, Company In-Charge, or Department Head can manage teams and assign team leaders."

    def _resolve_department(self, obj):
        if obj is None:
            return None
        if obj.__class__.__name__ == "Department":
            return obj
        dept = getattr(obj, "department", None)
        if dept is not None:
            return dept
        team = getattr(obj, "team", None)
        if team is not None:
            return getattr(team, "department", None)
        return None

    def _can_manage_department_teams(self, user, department) -> bool:
        if is_ceo(user):
            return True
        if department is None:
            return False

        membership = get_active_membership(user, company_id=department.company_id)
        if not membership:
            return False
        role_upper = membership.role.upper()
        if role_upper in DEPARTMENT_MANAGER_ROLES:
            return True

        from .models import DepartmentMembership

        # Active head of this exact department is always allowed to manage its teams
        if DepartmentMembership.objects.filter(
            department=department,
            user=user,
            status=DepartmentMembership.Status.ACTIVE,
            is_head=True,
        ).exists():
            return True

        # If user holds the DepartmentHead role in this company and isn't pinned exclusively
        # as head of another department, allow managing teams in this company
        if role_upper == SystemRole.DEPARTMENT_HEAD.upper():
            heads_other_dept = DepartmentMembership.objects.filter(
                department__company_id=department.company_id,
                user=user,
                status=DepartmentMembership.Status.ACTIVE,
                is_head=True,
            ).exclude(department=department).exists()
            return not heads_other_dept

        return False

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True

        dept_id = _extract_department_id_from_request(request, view)
        if dept_id:
            from .models import Department

            try:
                dept = Department.objects.select_related("company").filter(pk=dept_id).first()
            except Exception:
                # Let serializer return a clean 400 on malformed UUID
                dept = None
            if dept is not None:
                return self._can_manage_department_teams(user, dept)

        company_id = get_company_id_from_request(request, view)
        if company_id:
            membership = get_active_membership(user, company_id=company_id)
            if not membership:
                return False
            if membership.role.upper() in TEAM_MANAGER_ROLES:
                return True
            from .models import DepartmentMembership

            return DepartmentMembership.objects.filter(
                department__company_id=company_id,
                user=user,
                status=DepartmentMembership.Status.ACTIVE,
                is_head=True,
            ).exists()

        user_roles = CompanyMembership.objects.filter(
            user=user,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
        ).values_list("role", flat=True)
        if any(r.upper() in TEAM_MANAGER_ROLES for r in user_roles):
            return True

        from .models import DepartmentMembership

        return DepartmentMembership.objects.filter(
            user=user,
            status=DepartmentMembership.Status.ACTIVE,
            is_head=True,
        ).exists()

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True
        dept = self._resolve_department(obj)
        return self._can_manage_department_teams(user, dept)


class CanManageReportingRelationships(permissions.BasePermission):
    """
    Reporting-manager assignment permission (SRS §5.3 & DB Design §4.2):
    - CEO: full access across all companies.
    - Company In-Charge: can assign/update/end reporting managers within their active company.
    - Other roles and non-members receive 403 Forbidden.
    """

    message = "Only the CEO or Company In-Charge can manage reporting relationships."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True

        company_id = get_company_id_from_request(request, view)
        if company_id:
            membership = get_active_membership(user, company_id=company_id)
            if not membership:
                return False
            return membership.role.upper() in DEPARTMENT_MANAGER_ROLES

        user_roles = CompanyMembership.objects.filter(
            user=user,
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
        ).values_list("role", flat=True)
        return any(r.upper() in DEPARTMENT_MANAGER_ROLES for r in user_roles)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if is_ceo(user):
            return True

        company_id = get_company_id_from_request(request, view) or getattr(obj, "company_id", None)
        if company_id:
            membership = get_active_membership(user, company_id=company_id)
            if not membership:
                return False
            return membership.role.upper() in DEPARTMENT_MANAGER_ROLES

        # If target object is a User (e.g., /api/employees/<id>/reporting-manager/),
        # check that the acting user is CompanyInCharge in at least one shared active company
        if obj.__class__.__name__ == "User":
            target_cids = set(
                CompanyMembership.objects.filter(
                    user=obj,
                    is_active=True,
                    status=CompanyMembership.Status.ACTIVE,
                ).values_list("company_id", flat=True)
            )
            manager_cids = set(
                CompanyMembership.objects.filter(
                    user=user,
                    is_active=True,
                    status=CompanyMembership.Status.ACTIVE,
                    role__iexact=SystemRole.COMPANY_IN_CHARGE,
                ).values_list("company_id", flat=True)
            )
            return bool(target_cids & manager_cids)

        return False
