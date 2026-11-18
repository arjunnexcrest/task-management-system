from django.utils import timezone
import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from .models import User
from .serializers import (
    ActivateAccountSerializer,
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    CustomTokenRefreshSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    LogoutSerializer,
    RegisterEmployeeSerializer,
    ResetPasswordSerializer,
    UpdateMeSerializer,
    UserProfileSerializer,
    UserSerializer,
)


class IsActiveUser(BasePermission):
    message = "Your account is not active or is locked."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.can_login()


class IsStaffOrAdmin(BasePermission):
    message = "Only staff members can perform this action."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.is_staff or request.user.is_superuser


class UserFilter(django_filters.FilterSet):
    company_id = django_filters.UUIDFilter(
        field_name="company_memberships__company_id",
        distinct=True,
    )
    department_id = django_filters.UUIDFilter(
        field_name="department_memberships__department_id",
        distinct=True,
    )
    team_id = django_filters.UUIDFilter(
        field_name="team_memberships__team_id",
        distinct=True,
    )

    class Meta:
        model = User
        fields = [
            "account_status",
            "is_active",
            "is_staff",
            "company_id",
            "department_id",
            "team_id",
        ]


class LoginRateThrottle(AnonRateThrottle):
    scope = "login"


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        refresh = CustomTokenObtainPairSerializer.get_token(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserProfileSerializer(user, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )


class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]
    serializer_class = CustomTokenRefreshSerializer


class LogoutView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Successfully logged out."}, status=status.HTTP_200_OK)


class MeView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        return Response(
            UserProfileSerializer(request.user, context={"request": request}).data
        )

    def patch(self, request):
        serializer = UpdateMeSerializer(
            request.user, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            UserProfileSerializer(serializer.instance, context={"request": request}).data
        )


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated, IsActiveUser]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password changed successfully."}, status=status.HTTP_200_OK)


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "If that email is registered, a reset link has been sent."},
            status=status.HTTP_200_OK,
        )


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password has been reset successfully."}, status=status.HTTP_200_OK)


class ActivateAccountView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ActivateAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Account activated. You can now log in."}, status=status.HTTP_200_OK)


class RegisterEmployeeView(generics.CreateAPIView):
    permission_classes = [IsAuthenticated, IsActiveUser, IsStaffOrAdmin]
    serializer_class = RegisterEmployeeSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            UserProfileSerializer(user, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by("-created_at")
    serializer_class = UserSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = UserFilter
    search_fields = ["full_name", "email", "username", "employee_id", "designation", "phone"]
    ordering_fields = ["full_name", "created_at", "employee_id", "account_status"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser()]
        return [IsAuthenticated(), IsActiveUser(), IsStaffOrAdmin()]

    def perform_destroy(self, instance: User):
        instance.deactivate()

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate_account(self, request, pk=None):
        user = self.get_object()
        user.deactivate()
        return Response(
            {
                "detail": "User account deactivated.",
                "user": UserSerializer(user, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="activate")
    def activate_account(self, request, pk=None):
        user = self.get_object()
        user.is_active = True
        user.account_status = User.AccountStatus.ACTIVE
        user.save(update_fields=["is_active", "account_status", "updated_at"])
        return Response(
            {
                "detail": "User account activated.",
                "user": UserSerializer(user, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="suspend")
    def suspend_account(self, request, pk=None):
        user = self.get_object()
        user.is_active = False
        user.account_status = User.AccountStatus.SUSPENDED
        user.save(update_fields=["is_active", "account_status", "updated_at"])
        return Response(
            {
                "detail": "User account suspended.",
                "user": UserSerializer(user, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="unlock")
    def unlock_account(self, request, pk=None):
        user = self.get_object()
        user.clear_login_failures()
        return Response(
            {
                "detail": "User account unlocked.",
                "user": UserSerializer(user, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )
