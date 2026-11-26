import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.rbac.roles import SystemRole


class Company(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        INACTIVE = "Inactive", "Inactive"
        ARCHIVED = "Archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    code = models.CharField(max_length=50, unique=True, blank=True)
    email = models.EmailField(max_length=254, null=True, blank=True)
    phone = models.CharField(max_length=30, null=True, blank=True)
    logo = models.CharField(max_length=500, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    contact_information = models.JSONField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    working_calendar = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "companies"
        verbose_name = "Company"
        verbose_name_plural = "Companies"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if not self.code:
            base_code = slugify(self.name).upper().replace("-", "")[:12] or "COMP"
            candidate = base_code
            counter = 1
            while Company.objects.filter(code=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base_code}{counter}"
                counter += 1
            self.code = candidate
        if not self.is_active and self.status == self.Status.ACTIVE:
            self.status = self.Status.INACTIVE
        elif self.is_active and self.status == self.Status.INACTIVE:
            self.status = self.Status.ACTIVE
        super().save(*args, **kwargs)

    def soft_delete(self):
        """Soft delete the company by setting is_active=False without removing the row."""
        self.is_active = False
        self.status = self.Status.INACTIVE
        self.save(update_fields=["is_active", "status", "updated_at"])


class CompanyMembership(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        INACTIVE = "Inactive", "Inactive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="memberships",
        db_column="company_id",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="company_memberships",
        db_column="user_id",
    )
    role = models.CharField(
        max_length=40,
        choices=SystemRole.choices,
        default=SystemRole.EMPLOYEE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "company_memberships"
        verbose_name = "Company Membership"
        verbose_name_plural = "Company Memberships"
        constraints = [
            models.UniqueConstraint(fields=["company", "user"], name="uq_company_memberships_company_user"),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="idx_comp_members_user_status"),
            models.Index(fields=["user", "is_active"], name="idx_comp_members_user_active"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.company.code} ({self.role})"

    def save(self, *args, **kwargs):
        if not self.is_active:
            self.status = self.Status.INACTIVE
            if self.left_at is None:
                self.left_at = timezone.now()
        else:
            self.status = self.Status.ACTIVE
            self.left_at = None
        super().save(*args, **kwargs)

    def deactivate(self):
        """Soft-deactivate this company membership without deleting history."""
        self.is_active = False
        self.status = self.Status.INACTIVE
        self.left_at = timezone.now()
        self.save(update_fields=["is_active", "status", "left_at"])

    def activate(self):
        """Reactivate this company membership."""
        self.is_active = True
        self.status = self.Status.ACTIVE
        self.left_at = None
        self.save(update_fields=["is_active", "status", "left_at"])
