import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.rbac.roles import SystemRole


class UserQuerySet(models.QuerySet):
    def delete(self):
        count = self.update(
            is_active=False,
            account_status="Deactivated",
            updated_at=timezone.now(),
        )
        return count, {"accounts.User": count}

    def active(self):
        return self.filter(is_active=True, account_status="Active")


class UserManager(BaseUserManager):
    def get_queryset(self):
        return UserQuerySet(self.model, using=self._db)

    def active(self):
        return self.get_queryset().active()

    def create_user(self, email, username=None, full_name="", password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email).strip().lower()
        if not username:
            username = email.split("@")[0]
        else:
            username = username.strip()

        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("date_joined", timezone.now())
        user = self.model(email=email, username=username, full_name=full_name, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, username=None, full_name="Super Admin", password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("account_status", "Active")
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email=email, username=username, full_name=full_name, password=password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    class AccountStatus(models.TextChoices):
        ACTIVE = "Active", "Active"
        INACTIVE = "Inactive", "Inactive"
        PENDING_ACTIVATION = "PendingActivation", "Pending Activation"
        SUSPENDED = "Suspended", "Suspended"
        DEACTIVATED = "Deactivated", "Deactivated"

    RoleChoices = SystemRole

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee_id = models.CharField(max_length=50, blank=True, null=True, default="")
    full_name = models.CharField(max_length=160)
    email = models.EmailField(max_length=254, unique=True, db_index=True)
    username = models.CharField(max_length=150, unique=True, db_index=True)
    profile_image = models.CharField(max_length=500, null=True, blank=True)
    designation = models.CharField(max_length=120, null=True, blank=True)
    phone = models.CharField(max_length=30, null=True, blank=True)
    account_status = models.CharField(
        max_length=20,
        choices=AccountStatus.choices,
        default=AccountStatus.ACTIVE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    date_joined = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username", "full_name"]

    class Meta:
        db_table = "users"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} ({self.email})"

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        self.deactivate()
        return 1, {"accounts.User": 1}

    def deactivate(self):
        self.is_active = False
        self.account_status = self.AccountStatus.DEACTIVATED
        self.save(update_fields=["is_active", "account_status", "updated_at"])

    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        return timezone.now() < self.locked_until

    def can_login(self) -> bool:
        return (
            self.is_active
            and self.account_status == self.AccountStatus.ACTIVE
            and not self.is_locked
        )

    def record_failed_login(self, max_attempts: int = 5, lockout_minutes: int = 15):
        self.failed_login_attempts += 1
        if self.failed_login_attempts >= max_attempts:
            self.locked_until = timezone.now() + timezone.timedelta(minutes=lockout_minutes)
        self.save(update_fields=["failed_login_attempts", "locked_until", "updated_at"])

    def clear_login_failures(self):
        if self.failed_login_attempts != 0 or self.locked_until is not None:
            self.failed_login_attempts = 0
            self.locked_until = None
            self.save(update_fields=["failed_login_attempts", "locked_until", "updated_at"])
