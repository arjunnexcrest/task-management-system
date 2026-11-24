from datetime import date
from django.db.models import Q
from django.utils import timezone
import django_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from apps.common.permissions import IsCEO, IsCompanyMember, get_user_company_ids, is_ceo
from apps.organization.permissions import CanManageReportingRelationships
from .models import User
from .permissions import IsActiveUser, IsCEOOrSelf
from .serializers import (
    ActivateAccountSerializer,
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    CustomTokenRefreshSerializer,
    EmployeeProfileDetailSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    LogoutSerializer,
    RegisterEmployeeSerializer,
    ResetPasswordSerializer,
    UpdateMeSerializer,
    UserProfileSerializer,
    UserSerializer,
)


class UserFilter(django_filters.FilterSet):
    company = django_filters.UUIDFilter(method="filter_by_company")
    company_id = django_filters.UUIDFilter(method="filter_by_company")
    role = django_filters.CharFilter(method="filter_by_role")
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
            "company",
            "company_id",
            "role",
            "is_active",
            "account_status",
            "is_staff",
            "department_id",
            "team_id",
        ]

    def filter_by_company(self, queryset, name, value):
        return queryset.filter(
            company_memberships__company_id=value,
            company_memberships__is_active=True,
        ).distinct()

    def filter_by_role(self, queryset, name, value):
        return queryset.filter(
            Q(
                company_memberships__role__iexact=value,
                company_memberships__is_active=True,
            )
            | Q(
                company_memberships__membership_roles__role__code__iexact=value,
                company_memberships__is_active=True,
            )
        ).distinct()


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
    permission_classes = [IsAuthenticated, IsActiveUser, IsCEO]
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
    serializer_class = UserSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = UserFilter
    search_fields = ["full_name", "email", "username", "employee_id", "designation", "phone"]
    ordering_fields = ["full_name", "created_at", "employee_id", "account_status"]

    def get_queryset(self):
        qs = User.objects.prefetch_related("company_memberships__company").all().order_by("-created_at")
        user = self.request.user
        if is_ceo(user):
            return qs
        cids = get_user_company_ids(user)
        if not cids:
            return qs.filter(id=user.id) if (user and user.is_authenticated) else qs.none()
        return qs.filter(
            company_memberships__company_id__in=cids,
            company_memberships__is_active=True,
        ).distinct()

    def get_permissions(self):
        if self.action == "me":
            return [IsAuthenticated(), IsActiveUser()]
        if self.action in ("list", "retrieve", "direct_reports", "reporting_chain"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        if self.action == "profile":
            if self.request.method in ("PATCH", "PUT"):
                return [IsAuthenticated(), IsActiveUser(), IsCEOOrSelf()]
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        if self.action == "reporting_manager":
            if self.request.method == "GET":
                return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
            return [IsAuthenticated(), IsActiveUser(), CanManageReportingRelationships()]
        if self.action in ("update", "partial_update"):
            return [IsAuthenticated(), IsActiveUser(), IsCEOOrSelf()]
        return [IsAuthenticated(), IsActiveUser(), IsCEO()]

    def perform_destroy(self, instance: User):
        instance.deactivate()

    @action(detail=False, methods=["get", "patch"], url_path="me")
    def me(self, request):
        """GET/PATCH /api/users/me/ — Returns or updates the logged-in user's profile."""
        if request.method == "PATCH":
            serializer = UpdateMeSerializer(
                request.user, data=request.data, partial=True, context={"request": request}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(
                UserProfileSerializer(serializer.instance, context={"request": request}).data
            )
        return Response(
            UserProfileSerializer(request.user, context={"request": request}).data
        )

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate_account(self, request, pk=None):
        user = self.get_object()
        if user.pk == request.user.pk:
            return Response(
                {"detail": "You cannot deactivate your own currently signed-in account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
        if user.pk == request.user.pk:
            return Response(
                {"detail": "You cannot suspend your own currently signed-in account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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

    @action(detail=True, methods=["get", "patch"], url_path="profile")
    def profile(self, request, pk=None):
        """
        GET /api/employees/<id>/profile/ — Rich employee profile including memberships,
        departments, teams, active reporting managers, direct reports, upward chain, and history.
        PATCH /api/employees/<id>/profile/ — Update profile fields (CEO or self).
        """
        employee = self.get_object()
        if request.method == "PATCH":
            if is_ceo(request.user):
                serializer = UserSerializer(
                    employee, data=request.data, partial=True, context={"request": request}
                )
            else:
                serializer = UpdateMeSerializer(
                    employee, data=request.data, partial=True, context={"request": request}
                )
            serializer.is_valid(raise_exception=True)
            employee = serializer.save()
            if hasattr(employee, "_org_summary_cache"):
                delattr(employee, "_org_summary_cache")
        return Response(
            EmployeeProfileDetailSerializer(employee, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="direct-reports")
    def direct_reports(self, request, pk=None):
        """GET /api/employees/<id>/direct-reports/ — List active direct reports for this employee."""
        from apps.organization.models import ReportingRelationship

        manager = self.get_object()
        qs = ReportingRelationship.objects.filter(
            manager=manager, end_date__isnull=True
        ).select_related("employee", "company")
        company_id = request.query_params.get("company")
        if company_id:
            qs = qs.filter(company_id=company_id)
        data = [
            {
                "relationship_id": str(rel.id),
                "id": str(rel.employee.id),
                "full_name": rel.employee.full_name,
                "email": rel.employee.email,
                "employee_id": rel.employee.employee_id,
                "designation": rel.employee.designation,
                "company_id": str(rel.company_id),
                "company_name": rel.company.name,
                "start_date": rel.start_date.isoformat() if rel.start_date else None,
            }
            for rel in qs.order_by("employee__full_name")
        ]
        return Response({"count": len(data), "results": data}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="reporting-chain")
    def reporting_chain(self, request, pk=None):
        """GET /api/employees/<id>/reporting-chain/ — Upward reporting path to top manager."""
        from apps.organization.models import ReportingRelationship

        employee = self.get_object()
        company_id = request.query_params.get("company")
        chain = []
        visited = {employee.id}
        curr_id = employee.id
        level = 1
        while level <= 20:
            qs = ReportingRelationship.objects.filter(
                employee_id=curr_id, end_date__isnull=True
            ).select_related("manager", "company")
            if company_id:
                qs = qs.filter(company_id=company_id)
            rel = qs.order_by("-start_date").first()
            if not rel or rel.manager_id in visited:
                break
            visited.add(rel.manager_id)
            chain.append(
                {
                    "level": level,
                    "relationship_id": str(rel.id),
                    "id": str(rel.manager.id),
                    "full_name": rel.manager.full_name,
                    "email": rel.manager.email,
                    "designation": rel.manager.designation,
                    "company_id": str(rel.company_id),
                    "company_name": rel.company.name,
                }
            )
            curr_id = rel.manager_id
            level += 1
        return Response({"employee_id": str(employee.id), "chain": chain}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get", "post", "delete"], url_path="reporting-manager")
    def reporting_manager(self, request, pk=None):
        """
        GET /api/employees/<id>/reporting-manager/ — Active reporting manager(s) for this employee.
        POST /api/employees/<id>/reporting-manager/ — Assign/change (or clear with manager=null)
            the active reporting manager while preserving historical records.
        DELETE /api/employees/<id>/reporting-manager/?company=<uuid> — End active reporting manager.
        """
        from apps.companies.models import CompanyMembership
        from apps.organization.models import ReportingRelationship
        from apps.organization.serializers import ReportingRelationshipSerializer

        employee = self.get_object()

        if request.method == "GET":
            qs = ReportingRelationship.objects.filter(
                employee=employee, end_date__isnull=True
            ).select_related("company", "employee", "manager")
            company_id = request.query_params.get("company")
            if company_id:
                qs = qs.filter(company_id=company_id)
            return Response(
                ReportingRelationshipSerializer(qs, many=True).data,
                status=status.HTTP_200_OK,
            )

        company_id = request.data.get("company") or request.query_params.get("company")
        if not company_id:
            active_cids = list(
                CompanyMembership.objects.filter(
                    user=employee,
                    is_active=True,
                    status=CompanyMembership.Status.ACTIVE,
                ).values_list("company_id", flat=True)
            )
            if len(active_cids) == 1:
                company_id = str(active_cids[0])
            else:
                return Response(
                    {"company": ["Provide 'company' UUID to identify which company's reporting manager to update."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if request.method == "DELETE" or (
            "manager" in request.data and request.data.get("manager") in (None, "")
        ):
            ended_count = ReportingRelationship.objects.filter(
                company_id=company_id,
                employee=employee,
                end_date__isnull=True,
            ).update(end_date=date.today())
            return Response(
                {
                    "detail": "Active reporting manager ended.",
                    "ended_count": ended_count,
                },
                status=status.HTTP_200_OK,
            )

        manager_id = request.data.get("manager")
        if not manager_id:
            return Response(
                {"manager": ["Provide 'manager' user UUID, or null to end the current reporting manager."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = {
            "company": company_id,
            "employee": str(employee.id),
            "manager": manager_id,
        }
        if request.data.get("start_date"):
            payload["start_date"] = request.data["start_date"]

        serializer = ReportingRelationshipSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        relationship = serializer.save()
        return Response(
            ReportingRelationshipSerializer(relationship).data,
            status=status.HTTP_201_CREATED,
        )
