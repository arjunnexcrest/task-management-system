from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    SystemSettingViewSet,
    WorkflowStatusViewSet,
    WorkflowTransitionViewSet,
)

router = DefaultRouter()
router.register(r"workflow-statuses", WorkflowStatusViewSet, basename="workflow-status")
router.register(r"workflow-transitions", WorkflowTransitionViewSet, basename="workflow-transition")
router.register(r"", SystemSettingViewSet, basename="system-setting")

urlpatterns = [
    path("", include(router.urls)),
]
