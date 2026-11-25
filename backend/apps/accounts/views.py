from django.db.models import Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.common.permissions import UserManagementPermission
from .models import User
from .serializers import (
    ChangePasswordSerializer,
    LoginSerializer,
    MeUpdateSerializer,
    UserSerializer,
)


class LoginView(APIView):
    """
    Authenticates a user by work email or username and returns a JWT token pair
    together with the serialized User profile.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = serializer.validated_data["identifier"]
        password = serializer.validated_data["password"]

        user = (
            User.objects.filter(
                Q(email__iexact=identifier) | Q(username__iexact=identifier)
            )
            .first()
        )
        if user is None or not user.check_password(password):
            return Response(
                {"detail": "Invalid email/username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active or user.account_status != User.AccountStatus.ACTIVE:
            return Response(
                {
                    "detail": f"Account is {user.account_status.lower()} and cannot sign in."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """Stateless JWT logout endpoint compatible with the frontend client."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        return Response(
            {"detail": "Logged out successfully."},
            status=status.HTTP_200_OK,
        )


class MeView(APIView):
    """Retrieve or update the currently authenticated user's profile."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data, status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = MeUpdateSerializer(
            request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    """Allow an authenticated user to change their password."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not request.user.check_password(serializer.validated_data["old_password"]):
            return Response(
                {"detail": "Current password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password", "updated_at"])
        return Response(
            {"detail": "Password changed successfully."},
            status=status.HTTP_200_OK,
        )


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [UserManagementPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["account_status", "is_active", "is_staff"]
    search_fields = ["full_name", "email", "username", "employee_id", "designation"]
    ordering_fields = ["full_name", "created_at", "employee_id"]

    def get_queryset(self):
        qs = super().get_queryset()
        company_id = self.request.query_params.get("company") or self.request.query_params.get("company_id")
        role_code = self.request.query_params.get("role")
        department_id = self.request.query_params.get("department_id")
        team_id = self.request.query_params.get("team_id")

        if company_id:
            qs = qs.filter(
                company_memberships__company_id=company_id,
                company_memberships__status="Active",
            )
        if role_code:
            qs = qs.filter(
                company_memberships__status="Active",
                company_memberships__membership_roles__role__code__iexact=role_code,
            )
        if department_id:
            qs = qs.filter(
                department_memberships__department_id=department_id,
                department_memberships__status="Active",
            )
        if team_id:
            qs = qs.filter(
                team_memberships__team_id=team_id,
                team_memberships__status="Active",
            )
        return qs.distinct()

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self.get_object()
        user.account_status = User.AccountStatus.ACTIVE
        user.is_active = True
        user.save(update_fields=["account_status", "is_active", "updated_at"])
        return Response({"detail": "User activated.", "user": UserSerializer(user).data})

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        user.account_status = User.AccountStatus.DEACTIVATED
        user.is_active = False
        user.save(update_fields=["account_status", "is_active", "updated_at"])
        return Response({"detail": "User deactivated.", "user": UserSerializer(user).data})

    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        user = self.get_object()
        user.account_status = User.AccountStatus.SUSPENDED
        user.is_active = False
        user.save(update_fields=["account_status", "is_active", "updated_at"])
        return Response({"detail": "User suspended.", "user": UserSerializer(user).data})
