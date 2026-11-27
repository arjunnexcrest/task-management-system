from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from apps.common.permissions import IsActiveUser, IsCEO, is_ceo
from .models import Notification, NotificationDelivery, NotificationPreference
from .serializers import (
    NotificationDeliverySerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
)


class NotificationViewSet(viewsets.ModelViewSet):
    """
    User-isolated Notification API (SRS §13 & PERM-02):
    - Authenticated active users only see and manage their own notifications.
    - CEO can inspect and manage all notifications.
    """

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated, IsActiveUser]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["user", "notification_type", "is_read", "related_task", "related_project"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = (
            Notification.objects.select_related("user", "related_task", "related_project")
            .prefetch_related("deliveries")
            .all()
        )
        if is_ceo(self.request.user):
            return qs
        return qs.filter(user=self.request.user)

    def perform_create(self, serializer):
        target_user = serializer.validated_data.get("user")
        if not is_ceo(self.request.user):
            if target_user is not None and target_user.pk != self.request.user.pk:
                raise PermissionDenied("You cannot create notifications for another user.")
            serializer.save(user=self.request.user)
        else:
            serializer.save(user=target_user or self.request.user)

    def perform_update(self, serializer):
        target_user = serializer.validated_data.get("user")
        if not is_ceo(self.request.user):
            if target_user is not None and target_user.pk != self.request.user.pk:
                raise PermissionDenied("You cannot reassign a notification to another user.")
        serializer.save()

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(update_fields=["is_read", "read_at"])
        return Response(NotificationSerializer(notif).data)


class NotificationDeliveryViewSet(viewsets.ModelViewSet):
    """
    Notification delivery logs: users may only read their own deliveries;
    only CEO may create/update/delete delivery records.
    """

    serializer_class = NotificationDeliverySerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["notification", "channel", "delivery_status"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser()]
        return [IsAuthenticated(), IsActiveUser(), IsCEO()]

    def get_queryset(self):
        qs = NotificationDelivery.objects.select_related("notification", "notification__user").all()
        if is_ceo(self.request.user):
            return qs
        return qs.filter(notification__user=self.request.user)


class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    """
    Per-user notification preferences (SRS §13.3):
    non-CEO users can only read and modify their own preferences.
    """

    serializer_class = NotificationPreferenceSerializer
    permission_classes = [IsAuthenticated, IsActiveUser]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["user", "notification_type"]

    def get_queryset(self):
        qs = NotificationPreference.objects.select_related("user").all()
        if is_ceo(self.request.user):
            return qs
        return qs.filter(user=self.request.user)

    def perform_create(self, serializer):
        target_user = serializer.validated_data.get("user")
        if not is_ceo(self.request.user):
            if target_user is not None and target_user.pk != self.request.user.pk:
                raise PermissionDenied("You can only manage your own notification preferences.")
            serializer.save(user=self.request.user)
        else:
            serializer.save(user=target_user or self.request.user)

    def perform_update(self, serializer):
        target_user = serializer.validated_data.get("user")
        if not is_ceo(self.request.user):
            if target_user is not None and target_user.pk != self.request.user.pk:
                raise PermissionDenied("You can only manage your own notification preferences.")
        serializer.save()
