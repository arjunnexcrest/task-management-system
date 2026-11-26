import uuid
from django.conf import settings
from django.db import models

from .roles import SystemRole  # noqa: F401


class Role(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=80, unique=True)
    description = models.TextField(null=True, blank=True)
    is_system_role = models.BooleanField(default=False)

    class Meta:
        db_table = "roles"
        verbose_name = "Role"
        verbose_name_plural = "Roles"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Permission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=120, unique=True)
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "permissions"
        verbose_name = "Permission"
        verbose_name_plural = "Permissions"
        ordering = ["code"]

    def __str__(self):
        return self.code


class MembershipRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        "companies.CompanyMembership",
        on_delete=models.CASCADE,
        related_name="membership_roles",
        db_column="membership_id",
    )
    role = models.ForeignKey(
        Role,
        on_delete=models.PROTECT,
        related_name="membership_assignments",
        db_column="role_id",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_membership_roles",
        db_column="assigned_by_id",
    )

    class Meta:
        db_table = "membership_roles"
        verbose_name = "Membership Role"
        verbose_name_plural = "Membership Roles"
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "role"],
                name="uq_membership_roles_membership_role",
            ),
        ]

    def __str__(self):
        return f"{self.membership} -> {self.role.code}"


class RolePermission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="role_permissions",
        db_column="role_id",
    )
    permission = models.ForeignKey(
        Permission,
        on_delete=models.CASCADE,
        related_name="role_permissions",
        db_column="permission_id",
    )

    class Meta:
        db_table = "role_permissions"
        verbose_name = "Role Permission"
        verbose_name_plural = "Role Permissions"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "permission"],
                name="uq_role_permissions_role_perm",
            ),
        ]

    def __str__(self):
        return f"{self.role.code} -> {self.permission.code}"
