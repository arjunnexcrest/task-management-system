import uuid
from django.conf import settings
from django.db import models


class Company(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        INACTIVE = "Inactive", "Inactive"
        ARCHIVED = "Archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, unique=True)
    logo = models.CharField(max_length=500, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    contact_information = models.JSONField(null=True, blank=True)
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
        ]

    def __str__(self):
        return f"{self.user} @ {self.company.code}"
