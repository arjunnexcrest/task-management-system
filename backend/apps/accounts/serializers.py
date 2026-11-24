from django.contrib.auth import authenticate
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.views import exception_handler
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User


class AccountActivationTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return f"{user.pk}{timestamp}{user.is_active}{user.account_status}"


account_activation_token = AccountActivationTokenGenerator()
password_reset_token = PasswordResetTokenGenerator()


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        identifier = username.strip().lower()
        try:
            user = User.objects.get(
                Q(email__iexact=identifier) | Q(username__iexact=identifier)
            )
        except User.DoesNotExist:
            User().set_password(password)
            return None
        except User.MultipleObjectsReturned:
            return None
        if user.check_password(password):
            return user
        return None


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict):
        detail = response.data.get("detail")
        if isinstance(detail, list) and len(detail) == 1:
            response.data["detail"] = str(detail[0])
        elif "detail" not in response.data and "non_field_errors" in response.data:
            nfe = response.data.get("non_field_errors")
            if isinstance(nfe, list) and nfe:
                response.data["detail"] = str(nfe[0])
    return response


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user: User):
        token = super().get_token(user)
        token["user_id"] = str(user.id)
        token["email"] = user.email
        token["full_name"] = user.full_name
        token["account_status"] = user.account_status
        return token


class CustomTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        try:
            refresh = self.token_class(attrs["refresh"])
            user_id = refresh.payload.get("user_id")
            user = User.objects.filter(pk=user_id).first()
        except TokenError as exc:
            raise serializers.ValidationError({"detail": str(exc)})

        if user is None or not user.can_login():
            raise AuthenticationFailed(
                "Account is deactivated, inactive, or locked.",
                code="user_inactive",
            )

        return super().validate(attrs)


def _get_user_org_summary(obj: User) -> dict:
    roles: list[str] = []
    company_ids: list[str] = []
    memberships: list[dict] = []
    primary_company = None
    primary_company_name = None
    primary_role = "CEO" if obj.is_superuser else "Employee"
    department_names: list[str] = []
    team_names: list[str] = []
    reporting_managers: list[dict] = []
    direct_reports_count = 0

    try:
        from apps.companies.models import CompanyMembership
        from apps.organization.models import DepartmentMembership, ReportingRelationship, TeamMembership

        active_memberships = list(
            CompanyMembership.objects.filter(
                user=obj,
                is_active=True,
                status=CompanyMembership.Status.ACTIVE,
            )
            .select_related("company")
            .order_by("joined_at")
        )

        for m in active_memberships:
            company_ids.append(str(m.company_id))
            if m.role and m.role not in roles:
                roles.append(m.role)
            memberships.append(
                {
                    "id": str(m.id),
                    "company": str(m.company_id),
                    "company_name": m.company.name,
                    "company_code": m.company.code,
                    "role": m.role,
                    "is_active": m.is_active,
                    "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                }
            )

        if active_memberships:
            primary_company = str(active_memberships[0].company_id)
            primary_company_name = active_memberships[0].company.name
            primary_role = active_memberships[0].role

        department_names = list(
            DepartmentMembership.objects.filter(
                user=obj, status=DepartmentMembership.Status.ACTIVE
            )
            .select_related("department")
            .values_list("department__name", flat=True)
            .distinct()
        )

        team_names = list(
            TeamMembership.objects.filter(
                user=obj, status=TeamMembership.Status.ACTIVE
            )
            .select_related("team")
            .values_list("team__name", flat=True)
            .distinct()
        )

        for rel in (
            ReportingRelationship.objects.filter(employee=obj, end_date__isnull=True)
            .select_related("manager", "company")
        ):
            reporting_managers.append(
                {
                    "relationship_id": str(rel.id),
                    "id": str(rel.manager.id),
                    "full_name": rel.manager.full_name,
                    "email": rel.manager.email,
                    "designation": rel.manager.designation,
                    "company_id": str(rel.company_id),
                    "company_name": rel.company.name,
                    "start_date": rel.start_date.isoformat() if rel.start_date else None,
                }
            )

        direct_reports_count = ReportingRelationship.objects.filter(
            manager=obj, end_date__isnull=True
        ).count()
    except Exception:
        pass

    if not roles:
        roles = [primary_role]

    return {
        "memberships": memberships,
        "primary_company": primary_company,
        "primary_company_name": primary_company_name,
        "primary_role": primary_role,
        "roles": roles,
        "company_ids": company_ids,
        "department_names": department_names,
        "team_names": team_names,
        "reporting_managers": reporting_managers,
        "direct_reports_count": direct_reports_count,
    }


class UserProfileSerializer(serializers.ModelSerializer):
    company = serializers.SerializerMethodField()
    company_name = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    memberships = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    company_ids = serializers.SerializerMethodField()
    department_names = serializers.SerializerMethodField()
    team_names = serializers.SerializerMethodField()
    reporting_managers = serializers.SerializerMethodField()
    direct_reports_count = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "employee_id",
            "full_name",
            "email",
            "username",
            "company",
            "company_name",
            "role",
            "memberships",
            "profile_image",
            "designation",
            "phone",
            "account_status",
            "is_active",
            "is_staff",
            "is_superuser",
            "roles",
            "company_ids",
            "department_names",
            "team_names",
            "reporting_managers",
            "direct_reports_count",
            "date_joined",
            "created_at",
            "updated_at",
            "last_login",
        ]
        read_only_fields = fields

    def _ctx(self, obj: User) -> dict:
        cache_attr = "_org_summary_cache"
        if not hasattr(obj, cache_attr):
            setattr(obj, cache_attr, _get_user_org_summary(obj))
        return getattr(obj, cache_attr)

    def get_company(self, obj: User):
        return self._ctx(obj)["primary_company"]

    def get_company_name(self, obj: User):
        return self._ctx(obj)["primary_company_name"]

    def get_role(self, obj: User) -> str:
        return self._ctx(obj)["primary_role"]

    def get_memberships(self, obj: User) -> list[dict]:
        return self._ctx(obj)["memberships"]

    def get_roles(self, obj: User) -> list[str]:
        return self._ctx(obj)["roles"]

    def get_company_ids(self, obj: User) -> list[str]:
        return self._ctx(obj)["company_ids"]

    def get_department_names(self, obj: User) -> list[str]:
        return self._ctx(obj)["department_names"]

    def get_team_names(self, obj: User) -> list[str]:
        return self._ctx(obj)["team_names"]

    def get_reporting_managers(self, obj: User) -> list[dict]:
        return self._ctx(obj)["reporting_managers"]

    def get_direct_reports_count(self, obj: User) -> int:
        return self._ctx(obj)["direct_reports_count"]


class EmployeeProfileDetailSerializer(UserProfileSerializer):
    """
    Rich employee profile serializer returned by GET /api/employees/<id>/profile/
    (and /api/users/<id>/profile/): includes memberships, departments, teams,
    active reporting managers, direct reports, upward reporting chain, and reporting history.
    """

    departments = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()
    direct_reports = serializers.SerializerMethodField()
    reporting_chain = serializers.SerializerMethodField()
    reporting_history = serializers.SerializerMethodField()

    class Meta(UserProfileSerializer.Meta):
        fields = UserProfileSerializer.Meta.fields + [
            "departments",
            "teams",
            "direct_reports",
            "reporting_chain",
            "reporting_history",
        ]
        read_only_fields = fields

    def get_departments(self, obj: User) -> list[dict]:
        from apps.organization.models import DepartmentMembership

        return [
            {
                "id": str(dm.id),
                "department_id": str(dm.department_id),
                "department_name": dm.department.name,
                "department_code": dm.department.code,
                "company_id": str(dm.department.company_id),
                "company_name": dm.department.company.name,
                "is_head": dm.is_head,
                "start_date": dm.start_date.isoformat() if dm.start_date else None,
            }
            for dm in DepartmentMembership.objects.filter(
                user=obj, status=DepartmentMembership.Status.ACTIVE
            ).select_related("department", "department__company")
        ]

    def get_teams(self, obj: User) -> list[dict]:
        from apps.organization.models import TeamMembership

        return [
            {
                "id": str(tm.id),
                "team_id": str(tm.team_id),
                "team_name": tm.team.name,
                "team_code": tm.team.code,
                "department_id": str(tm.team.department_id),
                "department_name": tm.team.department.name,
                "company_id": str(tm.team.department.company_id),
                "is_leader": tm.is_leader,
                "joined_at": tm.joined_at.isoformat() if tm.joined_at else None,
            }
            for tm in TeamMembership.objects.filter(
                user=obj, status=TeamMembership.Status.ACTIVE
            ).select_related("team", "team__department")
        ]

    def get_direct_reports(self, obj: User) -> list[dict]:
        from apps.organization.models import ReportingRelationship

        return [
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
            for rel in ReportingRelationship.objects.filter(
                manager=obj, end_date__isnull=True
            )
            .select_related("employee", "company")
            .order_by("employee__full_name")
        ]

    def get_reporting_chain(self, obj: User) -> list[dict]:
        from apps.organization.models import ReportingRelationship

        chain: list[dict] = []
        visited = {obj.id}
        curr_id = obj.id
        level = 1
        while level <= 20:
            rel = (
                ReportingRelationship.objects.filter(
                    employee_id=curr_id, end_date__isnull=True
                )
                .select_related("manager", "company")
                .order_by("-start_date")
                .first()
            )
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
        return chain

    def get_reporting_history(self, obj: User) -> list[dict]:
        from apps.organization.models import ReportingRelationship

        return [
            {
                "id": str(rel.id),
                "company_id": str(rel.company_id),
                "company_name": rel.company.name,
                "manager_id": str(rel.manager_id),
                "manager_name": rel.manager.full_name,
                "manager_email": rel.manager.email,
                "start_date": rel.start_date.isoformat() if rel.start_date else None,
                "end_date": rel.end_date.isoformat() if rel.end_date else None,
                "is_active": rel.end_date is None,
            }
            for rel in ReportingRelationship.objects.filter(employee=obj)
            .select_related("manager", "company")
            .order_by("-start_date")
        ]


class UpdateMeSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["full_name", "phone", "designation", "profile_image"]

    def update(self, instance, validated_data):
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        instance.save(update_fields=list(validated_data.keys()) + ["updated_at"])
        return instance


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(write_only=True)
    password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )

    def validate(self, attrs):
        identifier = attrs["identifier"].strip()
        password = attrs["password"]

        db_user = User.objects.filter(
            Q(email__iexact=identifier) | Q(username__iexact=identifier)
        ).first()

        if db_user is not None and db_user.is_locked:
            raise serializers.ValidationError(
                {"detail": "Account is temporarily locked due to multiple failed login attempts. Please try again in 15 minutes."},
                code="account_locked",
            )

        user = authenticate(
            request=self.context.get("request"),
            username=identifier,
            password=password,
        )

        if user is None:
            if db_user is not None:
                db_user.record_failed_login()
                if db_user.is_locked:
                    raise serializers.ValidationError(
                        {"detail": "Account is temporarily locked due to multiple failed login attempts. Please try again in 15 minutes."},
                        code="account_locked",
                    )

            raise serializers.ValidationError(
                {"detail": "Invalid credentials"},
                code="invalid_credentials",
            )

        if not user.is_active or user.account_status != User.AccountStatus.ACTIVE:
            raise serializers.ValidationError(
                {"detail": "Your account is not active. Please contact your administrator."},
                code="account_inactive",
            )

        user.clear_login_failures()
        attrs["user"] = user
        return attrs


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(write_only=True)

    def validate(self, attrs):
        self.token = attrs["refresh"]
        return attrs

    def save(self, **kwargs):
        try:
            token = RefreshToken(self.token)
            token.blacklist()
        except TokenError:
            raise serializers.ValidationError({"detail": "Invalid or already blacklisted token."})


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        user = self.context["request"].user
        try:
            validate_password(value, user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])
        return user


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.strip().lower()

    def save(self, **kwargs):
        from django.conf import settings
        from django.core.mail import send_mail

        email = self.validated_data["email"]
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            return None

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = password_reset_token.make_token(user)
        reset_url = f"{settings.FRONTEND_URL}/reset-password?uid={uid}&token={token}"

        send_mail(
            subject="Password Reset — Task Management System",
            message=f"Click the link to reset your password:\n\n{reset_url}\n\nThis link expires soon.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
        return {"uid": uid, "token": token}


class ResetPasswordSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    def validate(self, attrs):
        try:
            uid_decoded = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=uid_decoded)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            raise serializers.ValidationError({"detail": "Invalid reset link."})

        if not password_reset_token.check_token(user, attrs["token"]):
            raise serializers.ValidationError({"detail": "Reset link is invalid or has expired."})

        try:
            validate_password(attrs["new_password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)})

        attrs["user"] = user
        return attrs

    def save(self, **kwargs):
        user = self.validated_data["user"]
        user.set_password(self.validated_data["new_password"])
        user.failed_login_attempts = 0
        user.locked_until = None
        user.save(update_fields=["password", "failed_login_attempts", "locked_until", "updated_at"])
        return user


class ActivateAccountSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(
        write_only=True, required=False, min_length=8, trim_whitespace=False,
    )

    def validate(self, attrs):
        try:
            uid_decoded = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=uid_decoded)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            raise serializers.ValidationError({"detail": "Invalid activation link."})

        if not account_activation_token.check_token(user, attrs["token"]):
            raise serializers.ValidationError({"detail": "Activation link is invalid or has expired."})

        if user.account_status == User.AccountStatus.ACTIVE and user.is_active:
            raise serializers.ValidationError({"detail": "Account is already active."})

        if "password" in attrs and attrs["password"]:
            try:
                validate_password(attrs["password"], user=user)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"password": list(exc.messages)})

        attrs["user"] = user
        return attrs

    def save(self, **kwargs):
        user = self.validated_data["user"]
        user.account_status = User.AccountStatus.ACTIVE
        user.is_active = True
        update_fields = ["account_status", "is_active", "updated_at"]
        if self.validated_data.get("password"):
            user.set_password(self.validated_data["password"])
            update_fields.append("password")
        user.save(update_fields=update_fields)
        return user


class RegisterEmployeeSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, min_length=8, trim_whitespace=False)
    send_activation = serializers.BooleanField(write_only=True, default=True)

    class Meta:
        model = User
        fields = [
            "employee_id",
            "full_name",
            "email",
            "username",
            "designation",
            "phone",
            "password",
            "send_activation",
        ]

    def validate_email(self, value):
        cleaned = value.strip().lower()
        qs = User.objects.filter(email__iexact=cleaned)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return cleaned

    def validate_username(self, value):
        return value.strip()

    def validate_password(self, value):
        if value:
            validate_password(value)
        return value

    def create(self, validated_data):
        from django.conf import settings
        from django.core.mail import send_mail

        send_activation = validated_data.pop("send_activation", True)
        password = validated_data.pop("password", None)

        if send_activation:
            validated_data["account_status"] = User.AccountStatus.PENDING_ACTIVATION
            validated_data["is_active"] = False
        else:
            validated_data["account_status"] = User.AccountStatus.ACTIVE
            validated_data["is_active"] = True

        user = User.objects.create_user(password=password, **validated_data)

        if send_activation:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = account_activation_token.make_token(user)
            activation_url = f"{settings.FRONTEND_URL}/activate?uid={uid}&token={token}"
            send_mail(
                subject="Welcome — Activate your TMS account",
                message=f"Click the link to activate your account:\n\n{activation_url}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=True,
            )

        return user


class UserSerializer(serializers.ModelSerializer):
    """
    Per DB Design §7 Rule #9: 'Do not expose password hashes or unrestricted storage keys through serializers.'
    Company and role are stored on CompanyMembership; optional write-only `company` & `role`
    fields are accepted on create/update for seamless compatibility with /api/users/.
    """

    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False, min_length=8, trim_whitespace=False)
    company = serializers.UUIDField(required=False, allow_null=True, write_only=True)
    role = serializers.ChoiceField(
        choices=User.RoleChoices.choices, required=False, write_only=True
    )
    company_name = serializers.SerializerMethodField()
    memberships = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    company_ids = serializers.SerializerMethodField()
    department_names = serializers.SerializerMethodField()
    team_names = serializers.SerializerMethodField()
    reporting_managers = serializers.SerializerMethodField()
    direct_reports_count = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "employee_id",
            "full_name",
            "email",
            "username",
            "password",
            "company",
            "company_name",
            "role",
            "memberships",
            "profile_image",
            "designation",
            "phone",
            "account_status",
            "is_active",
            "is_staff",
            "is_superuser",
            "failed_login_attempts",
            "locked_until",
            "roles",
            "company_ids",
            "department_names",
            "team_names",
            "reporting_managers",
            "direct_reports_count",
            "date_joined",
            "created_at",
            "updated_at",
            "last_login",
        ]
        read_only_fields = [
            "id",
            "company_name",
            "memberships",
            "is_superuser",
            "failed_login_attempts",
            "locked_until",
            "roles",
            "company_ids",
            "department_names",
            "team_names",
            "reporting_managers",
            "direct_reports_count",
            "date_joined",
            "created_at",
            "updated_at",
            "last_login",
        ]

    def _ctx(self, obj: User) -> dict:
        cache_attr = "_org_summary_cache"
        if not hasattr(obj, cache_attr):
            setattr(obj, cache_attr, _get_user_org_summary(obj))
        return getattr(obj, cache_attr)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        ctx = self._ctx(instance)
        data["company"] = ctx["primary_company"]
        data["role"] = ctx["primary_role"]
        return data

    def get_company_name(self, obj: User):
        return self._ctx(obj)["primary_company_name"]

    def get_memberships(self, obj: User) -> list[dict]:
        return self._ctx(obj)["memberships"]

    def get_roles(self, obj: User) -> list[str]:
        return self._ctx(obj)["roles"]

    def get_company_ids(self, obj: User) -> list[str]:
        return self._ctx(obj)["company_ids"]

    def get_department_names(self, obj: User) -> list[str]:
        return self._ctx(obj)["department_names"]

    def get_team_names(self, obj: User) -> list[str]:
        return self._ctx(obj)["team_names"]

    def get_reporting_managers(self, obj: User) -> list[dict]:
        return self._ctx(obj)["reporting_managers"]

    def get_direct_reports_count(self, obj: User) -> int:
        return self._ctx(obj)["direct_reports_count"]

    def validate_email(self, value):
        cleaned = value.strip().lower()
        qs = User.objects.filter(email__iexact=cleaned)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return cleaned

    def validate_username(self, value):
        if value:
            qs = User.objects.filter(username__iexact=value.strip())
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("A user with this username already exists.")
        return value.strip() if value else ""

    def _upsert_membership(self, user: User, company_id=None, role=None):
        if not company_id and not role:
            return
        from apps.companies.models import Company, CompanyMembership
        if company_id:
            if not Company.objects.filter(pk=company_id).exists():
                raise serializers.ValidationError({"company": "Company does not exist."})
            membership, created = CompanyMembership.objects.get_or_create(
                company_id=company_id,
                user=user,
                defaults={
                    "role": role or User.RoleChoices.EMPLOYEE,
                    "is_active": True,
                },
            )
            if not created:
                changed = False
                if role and membership.role != role:
                    membership.role = role
                    changed = True
                if not membership.is_active:
                    membership.is_active = True
                    changed = True
                if changed:
                    membership.save()
        elif role:
            membership = (
                CompanyMembership.objects.filter(user=user, is_active=True)
                .order_by("joined_at")
                .first()
            )
            if membership:
                membership.role = role
                membership.save()

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        company_id = validated_data.pop("company", None)
        role = validated_data.pop("role", None)
        if not validated_data.get("username"):
            base_uname = validated_data["email"].split("@")[0].strip().lower()
            candidate = base_uname
            counter = 1
            while User.objects.filter(username__iexact=candidate).exists():
                candidate = f"{base_uname}{counter}"
                counter += 1
            validated_data["username"] = candidate
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        self._upsert_membership(user, company_id=company_id, role=role)
        return user

    def update(self, instance, validated_data):
        from rest_framework.exceptions import PermissionDenied
        from apps.common.permissions import is_ceo

        request = self.context.get("request")
        company_id = validated_data.pop("company", None)
        role = validated_data.pop("role", None)

        if role is not None and request and not is_ceo(request.user):
            raise PermissionDenied("Only the CEO can change user roles.")

        password = validated_data.pop("password", None)
        if "username" in validated_data and not validated_data["username"]:
            validated_data.pop("username")
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        if password:
            instance.set_password(password)
        instance.save()
        self._upsert_membership(instance, company_id=company_id, role=role)
        if hasattr(instance, "_org_summary_cache"):
            delattr(instance, "_org_summary_cache")
        return instance
