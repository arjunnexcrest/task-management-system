import uuid
from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Department(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        INACTIVE = "Inactive", "Inactive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="departments",
        db_column="company_id",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "departments"
        verbose_name = "Department"
        verbose_name_plural = "Departments"
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="uq_departments_company_name"),
        ]

    def __str__(self):
        return f"{self.name} ({self.company.code})"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def deactivate(self):
        self.status = self.Status.INACTIVE
        self.save(update_fields=["status", "updated_at"])

    def activate(self):
        self.status = self.Status.ACTIVE
        self.save(update_fields=["status", "updated_at"])


class DepartmentMembership(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        TRANSFERRED = "Transferred", "Transferred"
        ENDED = "Ended", "Ended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="memberships",
        db_column="department_id",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="department_memberships",
        db_column="user_id",
    )
    is_head = models.BooleanField(default=False)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        db_table = "department_memberships"
        verbose_name = "Department Membership"
        verbose_name_plural = "Department Memberships"

    def __str__(self):
        return f"{self.user} in {self.department.name}"


class Team(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        INACTIVE = "Inactive", "Inactive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="teams",
        db_column="department_id",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "teams"
        verbose_name = "Team"
        verbose_name_plural = "Teams"
        constraints = [
            models.UniqueConstraint(fields=["department", "name"], name="uq_teams_department_name"),
        ]

    def __str__(self):
        return f"{self.name} ({self.department.name})"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    def deactivate(self):
        self.status = self.Status.INACTIVE
        self.save(update_fields=["status"])

    def activate(self):
        self.status = self.Status.ACTIVE
        self.save(update_fields=["status"])


class TeamMembership(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        LEFT = "Left", "Left"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name="memberships",
        db_column="team_id",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="team_memberships",
        db_column="user_id",
    )
    is_leader = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        db_table = "team_memberships"
        verbose_name = "Team Membership"
        verbose_name_plural = "Team Memberships"

    def __str__(self):
        return f"{self.user} in {self.team.name}"


class ReportingRelationship(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="reporting_relationships",
        db_column="company_id",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="manager_relationships",
        db_column="employee_id",
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="direct_reports",
        db_column="manager_id",
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "reporting_relationships"
        verbose_name = "Reporting Relationship"
        verbose_name_plural = "Reporting Relationships"
        constraints = [
            models.CheckConstraint(
                condition=~Q(employee=F("manager")),
                name="chk_reporting_no_self_report",
            ),
        ]

    def __str__(self):
        return f"{self.employee} reports to {self.manager}"
