from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from apps.common.permissions import RBACAndSettingsPermission
from .models import SystemSetting, WorkflowStatus, WorkflowTransition
from .serializers import (
    SystemSettingSerializer,
    WorkflowStatusSerializer,
    WorkflowTransitionSerializer,
)


class SystemSettingViewSet(viewsets.ModelViewSet):
    queryset = SystemSetting.objects.select_related("company", "updated_by").all()
    serializer_class = SystemSettingSerializer
    permission_classes = [RBACAndSettingsPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["company", "setting_key"]
    search_fields = ["setting_key"]


class WorkflowStatusViewSet(viewsets.ModelViewSet):
    queryset = WorkflowStatus.objects.select_related("company").all()
    serializer_class = WorkflowStatusSerializer
    permission_classes = [RBACAndSettingsPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["company", "entity_type", "is_active"]
    ordering_fields = ["sort_order"]


class WorkflowTransitionViewSet(viewsets.ModelViewSet):
    queryset = WorkflowTransition.objects.select_related("company", "from_status", "to_status", "role").all()
    serializer_class = WorkflowTransitionSerializer
    permission_classes = [RBACAndSettingsPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["company", "entity_type", "from_status", "to_status", "role"]
