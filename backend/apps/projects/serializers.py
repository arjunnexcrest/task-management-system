from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from apps.accounts.models import User
from apps.common.serializers import CompanyScopedValidationMixin
from apps.notifications.models import Notification
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from .models import (
    MilestoneDependency,
    MilestoneTask,
    Project,
    ProjectDepartment,
    ProjectMember,
    ProjectMilestone,
    ProjectPhase,
    ProjectTeam,
)
from .services import (
    assign_project_manager_service,
    assign_project_members_service,
    map_project_departments_service,
    map_project_teams_service,
    sync_project_team_members_service,
    update_project_member_participation_service,
)


class ProjectMemberSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)
    user_designation = serializers.CharField(source="user.designation", read_only=True, default=None)
    user_employee_id = serializers.CharField(source="user.employee_id", read_only=True)
    is_project_manager = serializers.SerializerMethodField()
    department_name = serializers.SerializerMethodField()
    team_names = serializers.SerializerMethodField()
    owned_tasks_count = serializers.SerializerMethodField()

    class Meta:
        model = ProjectMember
        fields = [
            "id",
            "project",
            "user",
            "user_full_name",
            "user_email",
            "user_designation",
            "user_employee_id",
            "is_project_manager",
            "department_name",
            "team_names",
            "owned_tasks_count",
            "role_in_project",
            "status",
            "joined_at",
            "left_at",
        ]
        read_only_fields = ["id", "joined_at"]
        validators = []

    def validate(self, attrs):
        project = attrs.get("project") or getattr(self.instance, "project", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)
        if project and user:
            self.validate_user_in_company(user, project.company, field_label="Project member")
        return attrs

    def create(self, validated_data):
        # Reactivate existing membership record if previously removed/inactive (DB Design §1 & §4.3)
        project = validated_data["project"]
        user = validated_data["user"]
        role = validated_data.get("role_in_project") or "Team Member"
        existing = ProjectMember.objects.filter(project=project, user=user).first()
        if existing:
            was_inactive = existing.status != ProjectMember.Status.ACTIVE
            existing.role_in_project = role
            existing.status = ProjectMember.Status.ACTIVE
            existing.left_at = None
            existing.save(update_fields=["role_in_project", "status", "left_at"])
            if was_inactive:
                Notification.objects.create(
                    user=user,
                    notification_type="PROJECT_MEMBER_ASSIGNED",
                    title=f"Project Participation Reactivated: {project.code}",
                    message=f"Your participation in project '{project.name}' is now Active as {role}.",
                    related_project=project,
                )
            return existing
        instance = super().create(validated_data)
        Notification.objects.create(
            user=user,
            notification_type="PROJECT_MEMBER_ASSIGNED",
            title=f"Assigned to Project: {project.code}",
            message=f"You have been assigned to project '{project.name}' as {instance.role_in_project or 'Team Member'}.",
            related_project=project,
        )
        return instance

    def update(self, instance, validated_data):
        role = validated_data.get("role_in_project")
        status_val = validated_data.get("status")
        try:
            return update_project_member_participation_service(
                member=instance,
                role_in_project=role,
                status_val=status_val,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            raise serializers.ValidationError({"detail": msg})

    def get_is_project_manager(self, obj):
        return bool(obj.project and obj.project.project_manager_id == obj.user_id)

    def get_department_name(self, obj):
        if not obj.project_id:
            return None
        dm = (
            DepartmentMembership.objects.filter(
                user_id=obj.user_id,
                department__company_id=obj.project.company_id,
                status=DepartmentMembership.Status.ACTIVE,
            )
            .select_related("department")
            .first()
        )
        return dm.department.name if dm else None

    def get_team_names(self, obj):
        if not obj.project_id:
            return []
        tms = TeamMembership.objects.filter(
            user_id=obj.user_id,
            team__department__company_id=obj.project.company_id,
            status=TeamMembership.Status.ACTIVE,
        ).select_related("team")
        return [tm.team.name for tm in tms]

    def get_owned_tasks_count(self, obj):
        if not obj.project_id:
            return 0
        return obj.project.tasks.filter(task_owner_id=obj.user_id).count()


class ProjectDepartmentSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True)
    department_head_id = serializers.SerializerMethodField()
    department_head_name = serializers.SerializerMethodField()

    class Meta:
        model = ProjectDepartment
        fields = [
            "id",
            "project",
            "department",
            "department_name",
            "department_code",
            "department_head_id",
            "department_head_name",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        project = attrs.get("project") or getattr(self.instance, "project", None)
        department = attrs.get("department") or getattr(self.instance, "department", None)
        if project and department:
            self.validate_department_in_company(department, project.company)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        instance = super().create(validated_data)
        # Coordinate active Department Head into ProjectMember (SRS §2.3 & §6.3 Step 5)
        head_mem = (
            DepartmentMembership.objects.filter(
                department=instance.department,
                is_head=True,
                status=DepartmentMembership.Status.ACTIVE,
            )
            .select_related("user")
            .first()
        )
        if head_mem:
            ProjectMember.objects.update_or_create(
                project=instance.project,
                user=head_mem.user,
                defaults={
                    "role_in_project": (
                        "Project Manager"
                        if head_mem.user_id == instance.project.project_manager_id
                        else f"Department Head ({instance.department.name})"
                    ),
                    "status": ProjectMember.Status.ACTIVE,
                    "left_at": None,
                },
            )
        return instance

    def _get_head(self, obj):
        return (
            DepartmentMembership.objects.filter(
                department_id=obj.department_id,
                is_head=True,
                status=DepartmentMembership.Status.ACTIVE,
            )
            .select_related("user")
            .first()
        )

    def get_department_head_id(self, obj):
        hm = self._get_head(obj)
        return str(hm.user_id) if hm else None

    def get_department_head_name(self, obj):
        hm = self._get_head(obj)
        return hm.user.full_name if hm else None


class ProjectTeamSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    team_name = serializers.CharField(source="team.name", read_only=True)
    team_code = serializers.CharField(source="team.code", read_only=True)
    department_id = serializers.UUIDField(source="team.department_id", read_only=True)
    department_name = serializers.CharField(source="team.department.name", read_only=True)
    team_leader_id = serializers.SerializerMethodField()
    team_leader_name = serializers.SerializerMethodField()
    active_members_count = serializers.SerializerMethodField()
    participating_members_count = serializers.SerializerMethodField()
    auto_add_team_members = serializers.BooleanField(
        required=False,
        default=False,
        write_only=True,
        help_text="When true, automatically enrolls all active members of this team as ProjectMember participants.",
    )

    class Meta:
        model = ProjectTeam
        fields = [
            "id",
            "project",
            "team",
            "team_name",
            "team_code",
            "department_id",
            "department_name",
            "team_leader_id",
            "team_leader_name",
            "active_members_count",
            "participating_members_count",
            "auto_add_team_members",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        validators = []

    def validate(self, attrs):
        project = attrs.get("project") or getattr(self.instance, "project", None)
        team = attrs.get("team") or getattr(self.instance, "team", None)
        if project and team:
            self.validate_team_in_company(team, project.company)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        auto_add_members = bool(validated_data.pop("auto_add_team_members", False))
        project = validated_data["project"]
        team = validated_data["team"]
        # Ensure team's department is a participating department (DB Design §4.3)
        ProjectDepartment.objects.get_or_create(project=project, department_id=team.department_id)
        instance, _ = ProjectTeam.objects.get_or_create(project=project, team=team)
        leader_mem = (
            TeamMembership.objects.filter(
                team=team,
                is_leader=True,
                status=TeamMembership.Status.ACTIVE,
            )
            .select_related("user")
            .first()
        )
        if leader_mem:
            existing_pm = ProjectMember.objects.filter(project=project, user=leader_mem.user).first()
            if existing_pm:
                if existing_pm.status != ProjectMember.Status.ACTIVE:
                    existing_pm.status = ProjectMember.Status.ACTIVE
                    existing_pm.left_at = None
                    existing_pm.save(update_fields=["status", "left_at"])
            else:
                ProjectMember.objects.create(
                    project=project,
                    user=leader_mem.user,
                    role_in_project=f"Team Leader ({team.name})",
                    status=ProjectMember.Status.ACTIVE,
                )
        if auto_add_members:
            sync_project_team_members_service(project=project, team_id=team.id)
        return instance

    def _get_leader(self, obj):
        return (
            TeamMembership.objects.filter(
                team_id=obj.team_id,
                is_leader=True,
                status=TeamMembership.Status.ACTIVE,
            )
            .select_related("user")
            .first()
        )

    def get_team_leader_id(self, obj):
        lm = self._get_leader(obj)
        return str(lm.user_id) if lm else None

    def get_team_leader_name(self, obj):
        lm = self._get_leader(obj)
        return lm.user.full_name if lm else None

    def get_active_members_count(self, obj):
        return TeamMembership.objects.filter(
            team_id=obj.team_id,
            status=TeamMembership.Status.ACTIVE,
        ).count()

    def get_participating_members_count(self, obj):
        team_user_ids = TeamMembership.objects.filter(
            team_id=obj.team_id,
            status=TeamMembership.Status.ACTIVE,
        ).values_list("user_id", flat=True)
        return ProjectMember.objects.filter(
            project_id=obj.project_id,
            user_id__in=team_user_ids,
            status=ProjectMember.Status.ACTIVE,
        ).count()


class ProjectPhaseSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    owner_name = serializers.CharField(source="owner.full_name", read_only=True, default=None)

    class Meta:
        model = ProjectPhase
        fields = [
            "id",
            "project",
            "name",
            "start_date",
            "end_date",
            "owner",
            "owner_name",
            "status",
            "progress",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        project = attrs.get("project") or getattr(self.instance, "project", None)
        owner = attrs.get("owner") if "owner" in attrs else getattr(self.instance, "owner", None)
        start_date = attrs.get("start_date") if "start_date" in attrs else getattr(self.instance, "start_date", None)
        end_date = attrs.get("end_date") if "end_date" in attrs else getattr(self.instance, "end_date", None)

        if project and owner:
            self.validate_user_in_company(owner, project.company, field_label="Phase owner")
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({"end_date": "Phase end_date cannot be earlier than start_date."})
        return attrs


class MilestoneTaskSerializer(serializers.ModelSerializer):
    task_title = serializers.CharField(source="task.title", read_only=True)
    task_status = serializers.CharField(source="task.status", read_only=True)

    class Meta:
        model = MilestoneTask
        fields = ["id", "milestone", "task", "task_title", "task_status"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        milestone = attrs.get("milestone") or getattr(self.instance, "milestone", None)
        task = attrs.get("task") or getattr(self.instance, "task", None)
        if milestone and task and milestone.project_id != task.project_id:
            raise serializers.ValidationError("Milestone and linked task must belong to the same project (DB Design §4.3).")
        return attrs


class MilestoneDependencySerializer(serializers.ModelSerializer):
    depends_on_title = serializers.CharField(source="depends_on_milestone.title", read_only=True)

    class Meta:
        model = MilestoneDependency
        fields = ["id", "milestone", "depends_on_milestone", "depends_on_title"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        milestone = attrs.get("milestone") or getattr(self.instance, "milestone", None)
        depends_on = attrs.get("depends_on_milestone") or getattr(self.instance, "depends_on_milestone", None)
        if milestone and depends_on:
            if milestone.id == depends_on.id:
                raise serializers.ValidationError("A milestone cannot depend on itself.")
            if milestone.project_id != depends_on.project_id:
                raise serializers.ValidationError("Both milestones must belong to the same project.")

            # Cycle check
            visited = {milestone.id}
            queue = [depends_on.id]
            while queue:
                current_id = queue.pop(0)
                if current_id in visited:
                    raise serializers.ValidationError("Circular milestone dependency detected.")
                visited.add(current_id)
                prereq_ids = MilestoneDependency.objects.filter(
                    milestone_id=current_id
                ).values_list("depends_on_milestone_id", flat=True)
                queue.extend(prereq_ids)
        return attrs


class ProjectMilestoneSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    owner_name = serializers.CharField(source="owner.full_name", read_only=True, default=None)
    milestone_tasks = MilestoneTaskSerializer(many=True, read_only=True)
    dependencies = MilestoneDependencySerializer(many=True, read_only=True)

    class Meta:
        model = ProjectMilestone
        fields = [
            "id",
            "project",
            "title",
            "description",
            "target_date",
            "owner",
            "owner_name",
            "completion_criteria",
            "status",
            "approval_required",
            "progress",
            "milestone_tasks",
            "dependencies",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        project = attrs.get("project") or getattr(self.instance, "project", None)
        owner = attrs.get("owner") if "owner" in attrs else getattr(self.instance, "owner", None)
        if project and owner:
            self.validate_user_in_company(owner, project.company, field_label="Milestone owner")
        return attrs


class ProjectSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    project_manager_name = serializers.CharField(source="project_manager.full_name", read_only=True, default=None)
    tasks_count = serializers.SerializerMethodField()
    completed_tasks_count = serializers.SerializerMethodField()

    # SRS §6.1 Participating Departments, Teams, Members, and Initial Milestones/Phases
    department_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="List of Department UUIDs participating in this project.",
    )
    team_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="List of Team UUIDs participating in this project.",
    )
    member_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
        help_text="List of User UUIDs assigned as members of this project.",
    )
    initial_milestones = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        write_only=True,
        help_text="Optional list of milestones to create atomically during project creation.",
    )
    initial_phases = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        write_only=True,
        help_text="Optional list of project phases to create atomically during project creation.",
    )

    departments_summary = serializers.SerializerMethodField()
    teams_summary = serializers.SerializerMethodField()
    members_count = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id",
            "company",
            "company_name",
            "company_code",
            "project_manager",
            "project_manager_name",
            "name",
            "code",
            "description",
            "start_date",
            "planned_completion_date",
            "priority",
            "status",
            "objectives",
            "deliverables",
            "budget",
            "progress",
            "created_at",
            "closed_at",
            "closure_remarks",
            "tasks_count",
            "completed_tasks_count",
            "department_ids",
            "team_ids",
            "member_ids",
            "initial_milestones",
            "initial_phases",
            "departments_summary",
            "teams_summary",
            "members_count",
        ]
        read_only_fields = ["id", "created_at", "closed_at"]

    def validate(self, attrs):
        company = attrs.get("company") or getattr(self.instance, "company", None)
        project_manager = (
            attrs.get("project_manager")
            if "project_manager" in attrs
            else getattr(self.instance, "project_manager", None)
        )
        status_val = attrs.get("status") or getattr(self.instance, "status", Project.Status.DRAFT)
        code_val = attrs.get("code") or getattr(self.instance, "code", None)
        start_date = attrs.get("start_date") if "start_date" in attrs else getattr(self.instance, "start_date", None)
        planned_completion_date = (
            attrs.get("planned_completion_date")
            if "planned_completion_date" in attrs
            else getattr(self.instance, "planned_completion_date", None)
        )

        if company and code_val:
            dup_qs = Project.objects.filter(company=company, code__iexact=code_val)
            if self.instance:
                dup_qs = dup_qs.exclude(pk=self.instance.pk)
            if dup_qs.exists():
                raise serializers.ValidationError(
                    {"code": f"Project code '{code_val}' already exists in {company.name}."}
                )

        if status_val == Project.Status.ACTIVE and not project_manager:
            raise serializers.ValidationError(
                {"project_manager": "Active projects require an accountable project manager (DB Design §4.3 & §5)."}
            )
        if company and project_manager:
            self.validate_user_in_company(project_manager, company, field_label="Project manager")
        if start_date and planned_completion_date and planned_completion_date < start_date:
            raise serializers.ValidationError(
                {"planned_completion_date": "Planned completion date cannot be earlier than start date."}
            )

        # Validate Participating Departments belong to the selected Company
        if company and "department_ids" in attrs:
            dept_ids = attrs["department_ids"]
            if dept_ids:
                valid_depts = Department.objects.filter(id__in=dept_ids, company=company).count()
                if valid_depts != len(set(dept_ids)):
                    raise serializers.ValidationError(
                        {"department_ids": "All participating departments must belong to the project's company."}
                    )

        # Validate Participating Teams belong to the selected Company
        if company and "team_ids" in attrs:
            team_ids = attrs["team_ids"]
            if team_ids:
                valid_teams = Team.objects.filter(id__in=team_ids, department__company=company).count()
                if valid_teams != len(set(team_ids)):
                    raise serializers.ValidationError(
                        {"team_ids": "All participating teams must belong to the project's company."}
                    )

        # Validate Project Members belong to the selected Company
        if company and "member_ids" in attrs:
            for user_obj in User.objects.filter(id__in=attrs["member_ids"]):
                self.validate_user_in_company(user_obj, company, field_label="Project member")

        return attrs

    def _sync_project_associations(
        self,
        project: Project,
        department_ids=None,
        team_ids=None,
        member_ids=None,
        initial_phases=None,
        initial_milestones=None,
    ):
        if project.project_manager_id:
            assign_project_manager_service(
                project=project,
                project_manager_id=project.project_manager_id,
            )

        if department_ids is not None:
            map_project_departments_service(
                project=project,
                department_ids=department_ids,
                replace=True,
                auto_add_heads=True,
            )

        if team_ids is not None:
            map_project_teams_service(
                project=project,
                team_ids=team_ids,
                replace=True,
                auto_map_departments=True,
                auto_add_leaders=True,
            )

        if member_ids is not None:
            assign_project_members_service(
                project=project,
                member_ids=member_ids,
                replace=True,
            )

        if initial_phases:
            for ph in initial_phases:
                ph_name = (ph.get("name") or "").strip()
                if ph_name:
                    ProjectPhase.objects.create(
                        project=project,
                        name=ph_name,
                        start_date=ph.get("start_date") or None,
                        end_date=ph.get("end_date") or None,
                        owner_id=ph.get("owner") or project.project_manager_id,
                        status=ph.get("status") or ProjectPhase.Status.NOT_STARTED,
                    )

        if initial_milestones:
            for ms in initial_milestones:
                ms_title = (ms.get("title") or "").strip()
                ms_target = ms.get("target_date")
                if ms_title and ms_target:
                    ProjectMilestone.objects.create(
                        project=project,
                        title=ms_title,
                        description=ms.get("description") or None,
                        target_date=ms_target,
                        owner_id=ms.get("owner") or project.project_manager_id,
                        completion_criteria=ms.get("completion_criteria") or None,
                        approval_required=bool(ms.get("approval_required", False)),
                    )

    @transaction.atomic
    def create(self, validated_data):
        department_ids = validated_data.pop("department_ids", None)
        team_ids = validated_data.pop("team_ids", None)
        member_ids = validated_data.pop("member_ids", None)
        initial_phases = validated_data.pop("initial_phases", None)
        initial_milestones = validated_data.pop("initial_milestones", None)

        project = super().create(validated_data)
        self._sync_project_associations(
            project,
            department_ids=department_ids,
            team_ids=team_ids,
            member_ids=member_ids,
            initial_phases=initial_phases,
            initial_milestones=initial_milestones,
        )
        return project

    @transaction.atomic
    def update(self, instance, validated_data):
        department_ids = validated_data.pop("department_ids", None)
        team_ids = validated_data.pop("team_ids", None)
        member_ids = validated_data.pop("member_ids", None)
        validated_data.pop("initial_phases", None)
        validated_data.pop("initial_milestones", None)

        project = super().update(instance, validated_data)
        self._sync_project_associations(
            project,
            department_ids=department_ids,
            team_ids=team_ids,
            member_ids=member_ids,
        )
        return project

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["department_ids"] = [str(pd.department_id) for pd in instance.project_departments.all()]
        data["team_ids"] = [str(pt.team_id) for pt in instance.project_teams.all()]
        data["member_ids"] = [
            str(pm.user_id) for pm in instance.members.all() if pm.status == ProjectMember.Status.ACTIVE
        ]
        return data

    def get_departments_summary(self, obj):
        return [
            {
                "id": str(pd.id),
                "department_id": str(pd.department_id),
                "name": pd.department.name,
                "code": pd.department.code,
            }
            for pd in obj.project_departments.all()
        ]

    def get_teams_summary(self, obj):
        return [
            {
                "id": str(pt.id),
                "team_id": str(pt.team_id),
                "name": pt.team.name,
                "code": pt.team.code,
            }
            for pt in obj.project_teams.all()
        ]

    def get_members_count(self, obj):
        return sum(1 for pm in obj.members.all() if pm.status == ProjectMember.Status.ACTIVE)

    def get_tasks_count(self, obj):
        if hasattr(obj, "top_tasks_count"):
            return obj.top_tasks_count
        return obj.tasks.filter(parent_task__isnull=True).count()

    def get_completed_tasks_count(self, obj):
        if hasattr(obj, "done_tasks_count"):
            return obj.done_tasks_count
        return obj.tasks.filter(parent_task__isnull=True, status="Completed").count()


class ProjectDetailSerializer(ProjectSerializer):
    members = ProjectMemberSerializer(many=True, read_only=True)
    project_departments = ProjectDepartmentSerializer(many=True, read_only=True)
    project_teams = ProjectTeamSerializer(many=True, read_only=True)
    phases = ProjectPhaseSerializer(many=True, read_only=True)
    milestones = ProjectMilestoneSerializer(many=True, read_only=True)
    dashboard_summary = serializers.SerializerMethodField()

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + [
            "members",
            "project_departments",
            "project_teams",
            "phases",
            "milestones",
            "dashboard_summary",
        ]

    def get_dashboard_summary(self, obj):
        """
        SRS §6.5 Project Dashboard metrics:
        total, completed, in-progress, pending, overdue, blocked tasks, plus department & team progress.
        """
        today = timezone.now().date()
        all_tasks = list(obj.tasks.select_related("department", "team").all())
        top_tasks = [t for t in all_tasks if t.parent_task_id is None]

        total_tasks = len(top_tasks)
        completed_tasks = sum(1 for t in top_tasks if t.status == "Completed")
        in_progress_tasks = sum(1 for t in top_tasks if t.status in ("In Progress", "In Review", "Approved"))
        pending_tasks = sum(1 for t in top_tasks if t.status in ("Backlog", "To Do", "Assigned", "On Hold", "Changes Requested"))
        blocked_tasks = sum(1 for t in top_tasks if t.status == "Blocked")
        overdue_tasks = sum(
            1
            for t in top_tasks
            if t.due_date and t.due_date < today and t.status not in ("Completed", "Cancelled")
        )

        dept_map = {}
        for pd in obj.project_departments.all():
            dept_tasks = [t for t in top_tasks if t.department_id == pd.department_id]
            avg_prog = (
                round(sum(float(t.progress) for t in dept_tasks) / len(dept_tasks), 1)
                if dept_tasks
                else 0.0
            )
            dept_map[str(pd.department_id)] = {
                "department_id": str(pd.department_id),
                "department_name": pd.department.name,
                "total_tasks": len(dept_tasks),
                "completed_tasks": sum(1 for t in dept_tasks if t.status == "Completed"),
                "progress": avg_prog,
            }

        team_map = {}
        for pt in obj.project_teams.all():
            t_tasks = [t for t in top_tasks if t.team_id == pt.team_id]
            avg_prog = (
                round(sum(float(t.progress) for t in t_tasks) / len(t_tasks), 1)
                if t_tasks
                else 0.0
            )
            team_map[str(pt.team_id)] = {
                "team_id": str(pt.team_id),
                "team_name": pt.team.name,
                "total_tasks": len(t_tasks),
                "completed_tasks": sum(1 for t in t_tasks if t.status == "Completed"),
                "progress": avg_prog,
            }

        return {
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "in_progress_tasks": in_progress_tasks,
            "pending_tasks": pending_tasks,
            "blocked_tasks": blocked_tasks,
            "overdue_tasks": overdue_tasks,
            "department_progress": list(dept_map.values()),
            "team_progress": list(team_map.values()),
        }
