from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, viewsets
from rest_framework.permissions import IsAuthenticated
from apps.common.permissions import (
    IsActiveUser,
    IsCompanyMember,
    get_company_id_from_request,
    get_user_company_ids,
    is_ceo,
    is_user_active,
    user_has_any_role,
)
from apps.rbac.roles import SystemRole
from .models import SystemSetting, WorkflowStatus, WorkflowTransition
from .serializers import (
    SystemSettingSerializer,
    WorkflowStatusSerializer,
    WorkflowTransitionSerializer,
)

SETTINGS_ADMIN_ROLES = [SystemRole.CEO, SystemRole.COMPANY_IN_CHARGE]


class CanManageSettings(permissions.BasePermission):
    """
    SRS §18 & PERM-03:
    - Global settings / workflows (company=None): CEO only.
    - Company-scoped settings / workflows: CEO or Company In-Charge of that company.
    - Other roles receive 403 Forbidden on write operations.
    """

    message = "Only the CEO (global) or Company In-Charge (own company) can modify settings and workflows."

    def has_permission(self, request, view):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True
        if request.method in permissions.SAFE_METHODS:
            return len(get_user_company_ids(user)) > 0

        data = request.data if isinstance(request.data, dict) else {}
        if request.method == "POST":
            company_id = data.get("company") or data.get("company_id")
            if not company_id:
                # Global setting/workflow creation is CEO-only
                return False
            return user_has_any_role(user, SETTINGS_ADMIN_ROLES, company_id=company_id)

        # For PUT/PATCH/DELETE: if caller explicitly attempts to move record to global (company=None)
        # or another company, verify role in that target company immediately.
        if "company" in data or "company_id" in data:
            target_company = data.get("company") if "company" in data else data.get("company_id")
            if not target_company:
                return False
            return user_has_any_role(user, SETTINGS_ADMIN_ROLES, company_id=target_company)

        company_id = get_company_id_from_request(request, view)
        if company_id:
            return user_has_any_role(user, SETTINGS_ADMIN_ROLES, company_id=company_id)

        return user_has_any_role(user, SETTINGS_ADMIN_ROLES)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not is_user_active(user):
            return False
        if is_ceo(user):
            return True
        if request.method in permissions.SAFE_METHODS:
            if obj.company_id is None:
                return len(get_user_company_ids(user)) > 0
            return obj.company_id in get_user_company_ids(user)

        # Global record edits/deletes are strictly CEO-only
        if obj.company_id is None:
            return False
        return user_has_any_role(user, SETTINGS_ADMIN_ROLES, company_id=obj.company_id)


def _scope_settings_queryset(qs, request):
    user = request.user
    if not is_user_active(user):
        return qs.none()
    company_id = request.query_params.get("company") or request.headers.get("X-Company-ID")
    if is_ceo(user):
        return qs.filter(company_id=company_id) if company_id else qs

    user_cids = get_user_company_ids(user)
    if not user_cids:
        return qs.none()
    if company_id:
        if str(company_id) not in {str(c) for c in user_cids}:
            return qs.none()
        return qs.filter(Q(company_id=company_id) | Q(company__isnull=True))
    return qs.filter(Q(company__isnull=True) | Q(company_id__in=user_cids))


class SystemSettingViewSet(viewsets.ModelViewSet):
    serializer_class = SystemSettingSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["company", "setting_key"]
    search_fields = ["setting_key"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageSettings()]

    def get_queryset(self):
        qs = SystemSetting.objects.select_related("company", "updated_by").all()
        return _scope_settings_queryset(qs, self.request)

    def perform_create(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class WorkflowStatusViewSet(viewsets.ModelViewSet):
    serializer_class = WorkflowStatusSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["company", "entity_type", "is_active"]
    ordering_fields = ["sort_order"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageSettings()]

    def get_queryset(self):
        qs = WorkflowStatus.objects.select_related("company").all()
        return _scope_settings_queryset(qs, self.request)


class WorkflowTransitionViewSet(viewsets.ModelViewSet):
    serializer_class = WorkflowTransitionSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["company", "entity_type", "from_status", "to_status", "role"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageSettings()]

    def get_queryset(self):
        qs = WorkflowTransition.objects.select_related("company", "from_status", "to_status", "role").all()
        return _scope_settings_queryset(qs, self.request)
