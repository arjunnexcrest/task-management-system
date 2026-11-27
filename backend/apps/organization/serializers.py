from datetime import date
from django.db import transaction
from rest_framework import serializers
from apps.accounts.models import User
from apps.common.serializers import CompanyScopedValidationMixin
from apps.companies.models import CompanyMembership
from .models import (
    Department,
    DepartmentMembership,
    ReportingRelationship,
    Team,
    TeamMembership,
)


def _validate_active_company_member(user, company, field_name: str, field_label: str):
    """
    Strictly verifies that `user` is active and holds an active CompanyMembership
    in `company` (or is the company's designated company_incharge).
    """
    if not user.is_active:
        raise serializers.ValidationError(
            {field_name: f"{field_label} '{user.full_name}' is inactive."}
        )
    if getattr(company, "company_incharge_id", None) == user.id:
        return
    is_member = CompanyMembership.objects.filter(
        company=company,
        user=user,
        is_active=True,
        status=CompanyMembership.Status.ACTIVE,
    ).exists()
    if not is_member:
        raise serializers.ValidationError(
            {
                field_name: (
                    f"{field_label} '{user.full_name}' is not an active member of "
                    f"company '{company.name}'."
                )
            }
        )


class DepartmentSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    department_head = serializers.UUIDField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text="Optional User UUID to assign as the Department Head (or null to unassign).",
    )
    department_head_id = serializers.SerializerMethodField()
    department_head_name = serializers.SerializerMethodField()
    department_head_email = serializers.SerializerMethodField()
    teams_count = serializers.SerializerMethodField()
    active_members_count = serializers.SerializerMethodField()
    mapped_projects_count = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id",
            "company",
            "company_name",
            "company_code",
            "name",
            "code",
            "status",
            "is_active",
            "department_head",
            "department_head_id",
            "department_head_name",
            "department_head_email",
            "teams_count",
            "active_members_count",
            "mapped_projects_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "is_active", "created_at", "updated_at"]

    def validate_name(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Department name cannot be blank.")
        return cleaned

    def validate_code(self, value):
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def validate(self, attrs):
        company = attrs.get("company") or getattr(self.instance, "company", None)
        if self.instance and "company" in attrs and attrs["company"].id != self.instance.company_id:
            raise serializers.ValidationError(
                {"company": "Moving a department to another company is not allowed."}
            )

        effective_status = attrs.get(
            "status",
            getattr(self.instance, "status", Department.Status.ACTIVE),
        )
        if company and effective_status == Department.Status.ACTIVE and not company.is_active:
            raise serializers.ValidationError(
                {"company": "Cannot create or activate a department under an inactive company."}
            )

        name = attrs.get("name") or getattr(self.instance, "name", None)
        if company and name:
            dup_qs = Department.objects.filter(company=company, name__iexact=name)
            if self.instance is not None:
                dup_qs = dup_qs.exclude(pk=self.instance.pk)
            if dup_qs.exists():
                raise serializers.ValidationError(
                    {"name": "A department with this name already exists in this company."}
                )

        head_id = attrs.get("department_head")
        if company and head_id:
            if effective_status == Department.Status.INACTIVE:
                raise serializers.ValidationError(
                    {"department_head": "Cannot assign a Department Head to an inactive department."}
                )
            head_user = User.objects.filter(id=head_id).first()
            if not head_user:
                raise serializers.ValidationError(
                    {"department_head": "Selected department head user does not exist."}
                )
            _validate_active_company_member(
                head_user,
                company,
                field_name="department_head",
                field_label="Department head",
            )
        return attrs

    def _sync_department_head(self, department: Department, head_user_id):
        active_heads = DepartmentMembership.objects.filter(
            department=department,
            status=DepartmentMembership.Status.ACTIVE,
            is_head=True,
        )
        if not head_user_id:
            active_heads.update(is_head=False)
        else:
            active_heads.exclude(user_id=head_user_id).update(is_head=False)
            membership = DepartmentMembership.objects.filter(
                department=department,
                user_id=head_user_id,
                status=DepartmentMembership.Status.ACTIVE,
            ).first()
            if membership:
                if not membership.is_head:
                    membership.is_head = True
                    membership.save(update_fields=["is_head"])
            else:
                DepartmentMembership.objects.create(
                    department=department,
                    user_id=head_user_id,
                    is_head=True,
                    start_date=date.today(),
                    status=DepartmentMembership.Status.ACTIVE,
                )

        # Invalidate prefetched memberships cache so immediate serialization reflects DB state
        if hasattr(department, "_prefetched_objects_cache"):
            department._prefetched_objects_cache.pop("memberships", None)

    @transaction.atomic
    def create(self, validated_data):
        head_id = validated_data.pop("department_head", None)
        dept = super().create(validated_data)
        if head_id:
            self._sync_department_head(dept, head_id)
        return dept

    @transaction.atomic
    def update(self, instance, validated_data):
        has_head = "department_head" in validated_data
        head_id = validated_data.pop("department_head", None)
        dept = super().update(instance, validated_data)
        if has_head:
            self._sync_department_head(dept, head_id)
        return dept

    def _get_active_head_membership(self, obj):
        for m in obj.memberships.all():
            if m.status == DepartmentMembership.Status.ACTIVE and m.is_head:
                return m
        return None

    def get_department_head_id(self, obj):
        m = self._get_active_head_membership(obj)
        return str(m.user_id) if m else None

    def get_department_head_name(self, obj):
        m = self._get_active_head_membership(obj)
        return m.user.full_name if m else None

    def get_department_head_email(self, obj):
        m = self._get_active_head_membership(obj)
        return m.user.email if m else None

    def get_teams_count(self, obj):
        return obj.teams.count()

    def get_active_members_count(self, obj):
        return sum(1 for m in obj.memberships.all() if m.status == DepartmentMembership.Status.ACTIVE)

    def get_mapped_projects_count(self, obj):
        return obj.project_departments.count()


class AssignDepartmentHeadSerializer(serializers.Serializer):
    """Input serializer for POST /departments/<id>/assign_head/ (and assign-head/)."""

    department_head = serializers.UUIDField(required=False, allow_null=True)
    user = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if "department_head" not in attrs and "user" not in attrs:
            raise serializers.ValidationError(
                {"department_head": "Provide 'department_head' (or 'user') UUID, or null to unassign."}
            )
        return attrs


class TransferEmployeeSerializer(serializers.Serializer):
    """Input serializer for POST /departments/<id>/transfer_employee/."""

    user = serializers.UUIDField(required=True)
    is_head = serializers.BooleanField(required=False, default=False)

    def validate_user(self, value):
        user_obj = User.objects.filter(id=value).first()
        if not user_obj:
            raise serializers.ValidationError("Selected user does not exist.")
        return user_obj

    def validate(self, attrs):
        department = self.context.get("department")
        if department is not None:
            if department.status == Department.Status.INACTIVE:
                raise serializers.ValidationError(
                    {"department": "Cannot transfer an employee into an inactive department."}
                )
            _validate_active_company_member(
                attrs["user"],
                department.company,
                field_name="user",
                field_label="Employee",
            )
        return attrs


class DepartmentMembershipSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = DepartmentMembership
        fields = [
            "id",
            "department",
            "department_name",
            "user",
            "user_full_name",
            "user_email",
            "is_head",
            "start_date",
            "end_date",
            "status",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        department = attrs.get("department") or getattr(self.instance, "department", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)
        effective_status = attrs.get(
            "status", getattr(self.instance, "status", DepartmentMembership.Status.ACTIVE)
        )
        if department and effective_status == DepartmentMembership.Status.ACTIVE:
            if department.status == Department.Status.INACTIVE:
                raise serializers.ValidationError(
                    {"department": "Cannot add an active member to an inactive department."}
                )
        if department and user:
            _validate_active_company_member(
                user,
                department.company,
                field_name="user",
                field_label="Department member",
            )
            if effective_status == DepartmentMembership.Status.ACTIVE:
                dup_qs = DepartmentMembership.objects.filter(
                    department=department,
                    user=user,
                    status=DepartmentMembership.Status.ACTIVE,
                )
                if self.instance is not None:
                    dup_qs = dup_qs.exclude(pk=self.instance.pk)
                if dup_qs.exists():
                    raise serializers.ValidationError(
                        {"user": "This user already has an active membership in this department."}
                    )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        if (
            validated_data.get("is_head")
            and validated_data.get("status", DepartmentMembership.Status.ACTIVE)
            == DepartmentMembership.Status.ACTIVE
        ):
            DepartmentMembership.objects.filter(
                department=validated_data["department"],
                status=DepartmentMembership.Status.ACTIVE,
                is_head=True,
            ).update(is_head=False)
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        effective_head = validated_data.get("is_head", instance.is_head)
        effective_status = validated_data.get("status", instance.status)
        if effective_head and effective_status == DepartmentMembership.Status.ACTIVE:
            DepartmentMembership.objects.filter(
                department=validated_data.get("department", instance.department),
                status=DepartmentMembership.Status.ACTIVE,
                is_head=True,
            ).exclude(pk=instance.pk).update(is_head=False)
        return super().update(instance, validated_data)


class TeamSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True)
    company_id = serializers.UUIDField(source="department.company_id", read_only=True)
    company_name = serializers.CharField(source="department.company.name", read_only=True)
    company_code = serializers.CharField(source="department.company.code", read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    team_leader = serializers.UUIDField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text="Optional User UUID to assign as the Team Leader (or null to unassign).",
    )
    team_leader_id = serializers.SerializerMethodField()
    team_leader_name = serializers.SerializerMethodField()
    team_leader_email = serializers.SerializerMethodField()
    active_members_count = serializers.SerializerMethodField()
    mapped_projects_count = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = [
            "id",
            "department",
            "department_name",
            "department_code",
            "company_id",
            "company_name",
            "company_code",
            "name",
            "code",
            "status",
            "is_active",
            "team_leader",
            "team_leader_id",
            "team_leader_name",
            "team_leader_email",
            "active_members_count",
            "mapped_projects_count",
            "created_at",
        ]
        read_only_fields = ["id", "is_active", "created_at"]

    def validate_name(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Team name cannot be blank.")
        return cleaned

    def validate_code(self, value):
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    def validate(self, attrs):
        department = attrs.get("department") or getattr(self.instance, "department", None)
        if self.instance and "department" in attrs:
            new_dept = attrs["department"]
            if new_dept.company_id != self.instance.department.company_id:
                raise serializers.ValidationError(
                    {"department": "Moving a team to a department in another company is not allowed."}
                )
            if new_dept.status == Department.Status.INACTIVE:
                raise serializers.ValidationError(
                    {"department": "Cannot move a team into an inactive department."}
                )
        elif not self.instance and department and department.status == Department.Status.INACTIVE:
            raise serializers.ValidationError(
                {"department": "Cannot create a team under an inactive department."}
            )

        name = attrs.get("name") or getattr(self.instance, "name", None)
        if department and name:
            dup_qs = Team.objects.filter(department=department, name__iexact=name)
            if self.instance is not None:
                dup_qs = dup_qs.exclude(pk=self.instance.pk)
            if dup_qs.exists():
                raise serializers.ValidationError(
                    {"name": "A team with this name already exists in this department."}
                )

        leader_id = attrs.get("team_leader")
        if department and leader_id:
            effective_status = attrs.get(
                "status",
                getattr(self.instance, "status", Team.Status.ACTIVE),
            )
            if effective_status == Team.Status.INACTIVE:
                raise serializers.ValidationError(
                    {"team_leader": "Cannot assign a Team Leader to an inactive team."}
                )
            leader_user = User.objects.filter(id=leader_id).first()
            if not leader_user:
                raise serializers.ValidationError(
                    {"team_leader": "Selected team leader user does not exist."}
                )
            _validate_active_company_member(
                leader_user,
                department.company,
                field_name="team_leader",
                field_label="Team leader",
            )
        return attrs

    def _sync_team_leader(self, team: Team, leader_user_id):
        active_leaders = TeamMembership.objects.filter(
            team=team,
            status=TeamMembership.Status.ACTIVE,
            is_leader=True,
        )
        if not leader_user_id:
            active_leaders.update(is_leader=False)
        else:
            active_leaders.exclude(user_id=leader_user_id).update(is_leader=False)
            membership = TeamMembership.objects.filter(
                team=team,
                user_id=leader_user_id,
                status=TeamMembership.Status.ACTIVE,
            ).first()
            if membership:
                if not membership.is_leader:
                    membership.is_leader = True
                    membership.save(update_fields=["is_leader"])
            else:
                TeamMembership.objects.create(
                    team=team,
                    user_id=leader_user_id,
                    is_leader=True,
                    status=TeamMembership.Status.ACTIVE,
                )
        if hasattr(team, "_prefetched_objects_cache"):
            team._prefetched_objects_cache.pop("memberships", None)

    @transaction.atomic
    def create(self, validated_data):
        leader_id = validated_data.pop("team_leader", None)
        team = super().create(validated_data)
        if leader_id:
            self._sync_team_leader(team, leader_id)
        return team

    @transaction.atomic
    def update(self, instance, validated_data):
        has_leader = "team_leader" in validated_data
        leader_id = validated_data.pop("team_leader", None)
        team = super().update(instance, validated_data)
        if has_leader:
            self._sync_team_leader(team, leader_id)
        return team

    def _get_active_leader_membership(self, obj):
        for m in obj.memberships.all():
            if m.status == TeamMembership.Status.ACTIVE and m.is_leader:
                return m
        return None

    def get_team_leader_id(self, obj):
        m = self._get_active_leader_membership(obj)
        return str(m.user_id) if m else None

    def get_team_leader_name(self, obj):
        m = self._get_active_leader_membership(obj)
        return m.user.full_name if m else None

    def get_team_leader_email(self, obj):
        m = self._get_active_leader_membership(obj)
        return m.user.email if m else None

    def get_active_members_count(self, obj):
        return sum(1 for m in obj.memberships.all() if m.status == TeamMembership.Status.ACTIVE)

    def get_mapped_projects_count(self, obj):
        return obj.project_teams.count()


class AssignTeamLeaderSerializer(serializers.Serializer):
    """Input serializer for POST /teams/<id>/assign_leader/ (and assign-leader/)."""

    team_leader = serializers.UUIDField(required=False, allow_null=True)
    user = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if "team_leader" not in attrs and "user" not in attrs:
            raise serializers.ValidationError(
                {"team_leader": "Provide 'team_leader' (or 'user') UUID, or null to unassign."}
            )
        return attrs


class ManageTeamMemberSerializer(serializers.Serializer):
    """Input serializer for POST /teams/<id>/add_member/ and /remove_member/."""

    user = serializers.UUIDField(required=True)
    is_leader = serializers.BooleanField(required=False, default=False)

    def validate_user(self, value):
        user_obj = User.objects.filter(id=value).first()
        if not user_obj:
            raise serializers.ValidationError("Selected user does not exist.")
        return user_obj

    def validate(self, attrs):
        team = self.context.get("team")
        require_active = self.context.get("require_active", True)
        if team is not None:
            if require_active and team.status == Team.Status.INACTIVE:
                raise serializers.ValidationError(
                    {"team": "Cannot modify members of an inactive team."}
                )
            if require_active:
                _validate_active_company_member(
                    attrs["user"],
                    team.department.company,
                    field_name="user",
                    field_label="Team member",
                )
        return attrs


class TeamMembershipSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    team_name = serializers.CharField(source="team.name", read_only=True)
    department_id = serializers.UUIDField(source="team.department_id", read_only=True)
    department_name = serializers.CharField(source="team.department.name", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = TeamMembership
        fields = [
            "id",
            "team",
            "team_name",
            "department_id",
            "department_name",
            "user",
            "user_full_name",
            "user_email",
            "is_leader",
            "joined_at",
            "left_at",
            "status",
        ]
        read_only_fields = ["id", "joined_at"]

    def validate(self, attrs):
        team = attrs.get("team") or getattr(self.instance, "team", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)
        effective_status = attrs.get(
            "status", getattr(self.instance, "status", TeamMembership.Status.ACTIVE)
        )
        if team and effective_status == TeamMembership.Status.ACTIVE:
            if team.status == Team.Status.INACTIVE:
                raise serializers.ValidationError(
                    {"team": "Cannot add an active member to an inactive team."}
                )
        if team and user:
            _validate_active_company_member(
                user,
                team.department.company,
                field_name="user",
                field_label="Team member",
            )
            if effective_status == TeamMembership.Status.ACTIVE:
                dup_qs = TeamMembership.objects.filter(
                    team=team,
                    user=user,
                    status=TeamMembership.Status.ACTIVE,
                )
                if self.instance is not None:
                    dup_qs = dup_qs.exclude(pk=self.instance.pk)
                if dup_qs.exists():
                    raise serializers.ValidationError(
                        {"user": "This user already has an active membership in this team."}
                    )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        if (
            validated_data.get("is_leader")
            and validated_data.get("status", TeamMembership.Status.ACTIVE)
            == TeamMembership.Status.ACTIVE
        ):
            TeamMembership.objects.filter(
                team=validated_data["team"],
                status=TeamMembership.Status.ACTIVE,
                is_leader=True,
            ).update(is_leader=False)
        return super().create(validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        effective_leader = validated_data.get("is_leader", instance.is_leader)
        effective_status = validated_data.get("status", instance.status)
        if effective_leader and effective_status == TeamMembership.Status.ACTIVE:
            TeamMembership.objects.filter(
                team=validated_data.get("team", instance.team),
                status=TeamMembership.Status.ACTIVE,
                is_leader=True,
            ).exclude(pk=instance.pk).update(is_leader=False)
        return super().update(instance, validated_data)


class ReportingRelationshipSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_email = serializers.EmailField(source="employee.email", read_only=True)
    employee_designation = serializers.CharField(source="employee.designation", read_only=True)
    manager_name = serializers.CharField(source="manager.full_name", read_only=True)
    manager_email = serializers.EmailField(source="manager.email", read_only=True)
    manager_designation = serializers.CharField(source="manager.designation", read_only=True)
    start_date = serializers.DateField(required=False, default=date.today)
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = ReportingRelationship
        fields = [
            "id",
            "company",
            "company_name",
            "company_code",
            "employee",
            "employee_name",
            "employee_email",
            "employee_designation",
            "manager",
            "manager_name",
            "manager_email",
            "manager_designation",
            "start_date",
            "end_date",
            "is_active",
        ]
        read_only_fields = ["id", "is_active"]

    def get_is_active(self, obj) -> bool:
        return obj.end_date is None

    def validate(self, attrs):
        company = attrs.get("company") or getattr(self.instance, "company", None)
        employee = attrs.get("employee") or getattr(self.instance, "employee", None)
        manager = attrs.get("manager") or getattr(self.instance, "manager", None)
        start_date = attrs.get("start_date") or getattr(self.instance, "start_date", None) or date.today()
        end_date = attrs.get("end_date") if "end_date" in attrs else getattr(self.instance, "end_date", None)

        if employee and manager and employee.id == manager.id:
            raise serializers.ValidationError(
                {"manager": "An employee cannot report to themselves (DB Design §4.2)."}
            )
        if end_date is not None and start_date is not None and end_date < start_date:
            raise serializers.ValidationError(
                {"end_date": "End date cannot be earlier than start date."}
            )
        if company and employee:
            _validate_active_company_member(
                employee,
                company,
                field_name="employee",
                field_label="Employee",
            )
        if company and manager:
            _validate_active_company_member(
                manager,
                company,
                field_name="manager",
                field_label="Manager",
            )

        # Prevent duplicate identical active reporting relationship
        if company and employee and manager and end_date is None:
            dup_qs = ReportingRelationship.objects.filter(
                company=company,
                employee=employee,
                manager=manager,
                end_date__isnull=True,
            )
            if self.instance is not None:
                dup_qs = dup_qs.exclude(pk=self.instance.pk)
            if dup_qs.exists():
                raise serializers.ValidationError(
                    {"manager": "This active reporting relationship already exists."}
                )

        # Cycle check in active reporting hierarchy (DB Design §4.2)
        if company and employee and manager and end_date is None:
            visited = {employee.id}
            queue = [manager.id]
            while queue:
                curr = queue.pop(0)
                if curr in visited:
                    raise serializers.ValidationError(
                        {"manager": "Circular reporting relationship detected (DB Design §4.2)."}
                    )
                visited.add(curr)
                upper_qs = ReportingRelationship.objects.filter(
                    company=company,
                    employee_id=curr,
                    end_date__isnull=True,
                )
                if self.instance is not None:
                    upper_qs = upper_qs.exclude(pk=self.instance.pk)
                queue.extend(upper_qs.values_list("manager_id", flat=True))
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        validated_data.setdefault("start_date", date.today())
        company = validated_data["company"]
        employee = validated_data["employee"]
        start_date = validated_data["start_date"]
        end_date = validated_data.get("end_date")

        # When assigning a new active reporting manager, close any prior active manager
        # for the same (company, employee) to preserve full historical audit trail.
        if end_date is None:
            ReportingRelationship.objects.filter(
                company=company,
                employee=employee,
                end_date__isnull=True,
            ).update(end_date=start_date)

        return super().create(validated_data)
