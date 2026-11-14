from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    ProgressAttachmentViewSet,
    ProgressUpdateViewSet,
    TimeEntryViewSet,
)

router = DefaultRouter()
router.register(r"progress-updates", ProgressUpdateViewSet, basename="progress-update")
router.register(r"progress-attachments", ProgressAttachmentViewSet, basename="progress-attachment")
router.register(r"time-entries", TimeEntryViewSet, basename="time-entry")

urlpatterns = [
    path("", include(router.urls)),
]
