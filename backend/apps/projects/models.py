import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


class Project(models.Model):
    class Priority(models.TextChoices):
        CRITICAL = "Critical", "Critical"
        HIGH = "High", "High"
        MEDIUM = "Medium", "Medium"
        LOW = "Low", "Low"

    class Status(models.TextChoices):
        DRAFT = "Draft", "Draft"
        PLANNED = "Planned", "Planned"
        ACTIVE = "Active", "Active"
        ON_HOLD = "On Hold", "On Hold"
        AT_RISK = "At Risk", "At Risk"
        DELAYED = "Delayed", "Delayed"
        COMPLETED = "Completed", "Completed"
        CANCELLED = "Cancelled", "Cancelled"
        ARCHIVED = "Archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="projects",
        db_column="company_id",
    )
    project_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_projects",
        db_column="project_manager_id",
    )
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=60)
    description = models.TextField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    planned_completion_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    objectives = models.TextField(null=True, blank=True)
    deliverables = models.TextField(null=True, blank=True)
    budget = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closure_remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "projects"
        verbose_name = "Project"
        verbose_name_plural = "Projects"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uq_projects_company_code"),
            models.CheckConstraint(
                condition=Q(progress__gte=0) & Q(progress__lte=100),
                name="chk_projects_progress_range",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status"], name="idx_projects_company_status"),
        ]

    def clean(self):
        if self.status == self.Status.ACTIVE and not self.project_manager_id:
            raise ValidationError("Every active project must have an accountable project manager (SRS §1.4 #2).")

    def recalculate_progress(self):
        from decimal import Decimal

        top_tasks = self.tasks.filter(parent_task__isnull=True).exclude(status="Cancelled")
        total = top_tasks.count()
        if total > 0:
            avg_progress = sum(float(t.progress) for t in top_tasks) / total
            self.progress = Decimal(f"{round(avg_progress, 2):.2f}")
        else:
            self.progress = Decimal("0.00")
        self.save(update_fields=["progress"])

    def __str__(self):
        return f"{self.name} [{self.code}]"


class ProjectMember(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "Active", "Active"
        INACTIVE = "Inactive", "Inactive"
        REMOVED = "Removed", "Removed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="members",
        db_column="project_id",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="project_memberships",
        db_column="user_id",
    )
    role_in_project = models.CharField(max_length=80, null=True, blank=True, default="Member")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "project_members"
        verbose_name = "Project Member"
        verbose_name_plural = "Project Members"
        constraints = [
            models.UniqueConstraint(fields=["project", "user"], name="uq_project_members_project_user"),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="idx_proj_members_user_status"),
        ]

    def __str__(self):
        return f"{self.user} in {self.project.code} ({self.role_in_project})"


class ProjectDepartment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="project_departments",
        db_column="project_id",
    )
    department = models.ForeignKey(
        "organization.Department",
        on_delete=models.PROTECT,
        related_name="project_departments",
        db_column="department_id",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project_departments"
        verbose_name = "Project Department"
        verbose_name_plural = "Project Departments"
        constraints = [
            models.UniqueConstraint(fields=["project", "department"], name="uq_project_departments_proj_dept"),
        ]

    def clean(self):
        if self.project_id and self.department_id and self.project.company_id != self.department.company_id:
            raise ValidationError("Department must belong to the same company as the project.")

    def __str__(self):
        return f"{self.project.code} -> {self.department.name}"


class ProjectTeam(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="project_teams",
        db_column="project_id",
    )
    team = models.ForeignKey(
        "organization.Team",
        on_delete=models.PROTECT,
        related_name="project_teams",
        db_column="team_id",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project_teams"
        verbose_name = "Project Team"
        verbose_name_plural = "Project Teams"
        constraints = [
            models.UniqueConstraint(fields=["project", "team"], name="uq_project_teams_proj_team"),
        ]

    def clean(self):
        if self.project_id and self.team_id and self.project.company_id != self.team.department.company_id:
            raise ValidationError("Team must belong to the same company as the project.")

    def __str__(self):
        return f"{self.project.code} -> {self.team.name}"


class ProjectPhase(models.Model):
    class Status(models.TextChoices):
        NOT_STARTED = "Not Started", "Not Started"
        IN_PROGRESS = "In Progress", "In Progress"
        COMPLETED = "Completed", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="phases",
        db_column="project_id",
    )
    name = models.CharField(max_length=150)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_phases",
        db_column="owner_id",
    )
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.NOT_STARTED)
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)

    class Meta:
        db_table = "project_phases"
        verbose_name = "Project Phase"
        verbose_name_plural = "Project Phases"
        ordering = ["start_date", "name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(progress__gte=0) & Q(progress__lte=100),
                name="chk_project_phases_progress_range",
            ),
        ]

    def __str__(self):
        return f"{self.project.code} - {self.name}"


class ProjectMilestone(models.Model):
    class Status(models.TextChoices):
        PENDING = "Pending", "Pending"
        IN_PROGRESS = "In Progress", "In Progress"
        COMPLETED = "Completed", "Completed"
        DELAYED = "Delayed", "Delayed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="milestones",
        db_column="project_id",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    target_date = models.DateField()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_milestones",
        db_column="owner_id",
    )
    completion_criteria = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    approval_required = models.BooleanField(default=False)
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)

    class Meta:
        db_table = "project_milestones"
        verbose_name = "Project Milestone"
        verbose_name_plural = "Project Milestones"
        ordering = ["target_date"]
        constraints = [
            models.CheckConstraint(
                condition=Q(progress__gte=0) & Q(progress__lte=100),
                name="chk_project_milestones_progress_range",
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.project.code})"


class MilestoneTask(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    milestone = models.ForeignKey(
        ProjectMilestone,
        on_delete=models.CASCADE,
        related_name="milestone_tasks",
        db_column="milestone_id",
    )
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="milestone_links",
        db_column="task_id",
    )

    class Meta:
        db_table = "milestone_tasks"
        verbose_name = "Milestone Task"
        verbose_name_plural = "Milestone Tasks"
        constraints = [
            models.UniqueConstraint(fields=["milestone", "task"], name="uq_milestone_tasks_milestone_task"),
        ]

    def clean(self):
        if self.milestone_id and self.task_id and self.milestone.project_id != self.task.project_id:
            raise ValidationError("Milestone and task must belong to the same project.")

    def __str__(self):
        return f"{self.milestone.title} -> {self.task.title}"


class MilestoneDependency(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    milestone = models.ForeignKey(
        ProjectMilestone,
        on_delete=models.CASCADE,
        related_name="dependencies",
        db_column="milestone_id",
    )
    depends_on_milestone = models.ForeignKey(
        ProjectMilestone,
        on_delete=models.CASCADE,
        related_name="dependent_milestones",
        db_column="depends_on_milestone_id",
    )

    class Meta:
        db_table = "milestone_dependencies"
        verbose_name = "Milestone Dependency"
        verbose_name_plural = "Milestone Dependencies"
        constraints = [
            models.UniqueConstraint(
                fields=["milestone", "depends_on_milestone"],
                name="uq_milestone_dependencies",
            ),
            models.CheckConstraint(
                condition=~Q(milestone=F("depends_on_milestone")),
                name="chk_milestone_no_self_dependency",
            ),
        ]

    def clean(self):
        if self.milestone_id and self.depends_on_milestone_id:
            if self.milestone_id == self.depends_on_milestone_id:
                raise ValidationError("A milestone cannot depend on itself.")
            if self.milestone.project_id != self.depends_on_milestone.project_id:
                raise ValidationError("Both milestones must belong to the same project.")

    def __str__(self):
        return f"{self.milestone.title} depends on {self.depends_on_milestone.title}"
