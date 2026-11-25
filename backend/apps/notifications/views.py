from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev, is_ceo
from .models import Notification, NotificationDelivery, NotificationPreference
from .serializers import (
    NotificationDeliverySerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
)


class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["user", "notification_type", "is_read", "related_task", "related_project"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        qs = (
            Notification.objects.select_related("user", "related_task", "related_project")
            .prefetch_related("deliveries")
            .order_by("-created_at")
        )
        user = getattr(self.request, "user", None)
        if user and user.is_authenticated:
            mine = str(self.request.query_params.get("mine", "")).lower() in ("true", "1", "yes")
            if not is_ceo(user) or mine:
                qs = qs.filter(user=user)
        return qs

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(update_fields=["is_read", "read_at"])
        return Response(NotificationSerializer(notif).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        qs = self.get_queryset().filter(is_read=False)
        if request.user and request.user.is_authenticated:
            qs = qs.filter(user=request.user)
        updated = qs.update(is_read=True, read_at=timezone.now())
        return Response({"marked_read": updated})

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        qs = self.get_queryset().filter(is_read=False)
        if request.user and request.user.is_authenticated:
            qs = qs.filter(user=request.user)
        return Response({"unread_count": qs.count()})


class NotificationDeliveryViewSet(viewsets.ModelViewSet):
    queryset = NotificationDelivery.objects.select_related("notification").all()
    serializer_class = NotificationDeliverySerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["notification", "channel", "delivery_status"]


class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    queryset = NotificationPreference.objects.select_related("user").all()
    serializer_class = NotificationPreferenceSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["user", "notification_type"]
