from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from .models import User
from .serializers import UserSerializer


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["account_status", "is_active", "is_staff"]
    search_fields = ["full_name", "email", "username", "employee_id", "designation"]
    ordering_fields = ["full_name", "created_at", "employee_id"]
