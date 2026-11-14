from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    NotificationDeliveryViewSet,
    NotificationPreferenceViewSet,
    NotificationViewSet,
)

router = DefaultRouter()
router.register(r"deliveries", NotificationDeliveryViewSet, basename="notification-delivery")
router.register(r"preferences", NotificationPreferenceViewSet, basename="notification-preference")
router.register(r"", NotificationViewSet, basename="notification")

urlpatterns = [
    path("", include(router.urls)),
]
