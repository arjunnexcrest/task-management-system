from datetime import date
from django.db import transaction
from rest_framework import serializers
from apps.accounts.models import User
from apps.common.serializers import CompanyScopedValidationMixin
from .models import (
    Department,
    DepartmentMembership,
    ReportingRelationship,
    Team,
    TeamMembership,
)


class DepartmentSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    department_head = serializers.UUIDField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text="Optional User UUID to assign as the Department Head.",
    )
    department_head_id = serializers.SerializerMethodField()
    department_head_name = serializers.SerializerMethodField()
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
            "department_head",
            "department_head_id",
            "department_head_name",
            "teams_count",
            "active_members_count",
            "mapped_projects_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        company = attrs.get("company") or getattr(self.instance, "company", None)
        head_id = attrs.get("department_head")
        if company and head_id:
            head_user = User.objects.filter(id=head_id).first()
            if not head_user:
                raise serializers.ValidationError({"department_head": "Selected department head user does not exist."})
            self.validate_user_in_company(head_user, company, field_label="Department head")
        return attrs

    def _sync_department_head(self, department: Department, head_user_id):
        if head_user_id is None:
            return
        DepartmentMembership.objects.filter(
            department=department,
            status=DepartmentMembership.Status.ACTIVE,
            is_head=True,
        ).exclude(user_id=head_user_id).update(is_head=False)
        if head_user_id:
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
            self._sync_department_head(dept, head_id or "")
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

    def get_teams_count(self, obj):
        return obj.teams.count()

    def get_active_members_count(self, obj):
        return sum(1 for m in obj.memberships.all() if m.status == DepartmentMembership.Status.ACTIVE)

    def get_mapped_projects_count(self, obj):
        return obj.project_departments.count()


class DepartmentMembershipSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = DepartmentMembership
        fields = [
            "id",
            "department",
            "department_name",
            "user",
            "user_full_name",
            "is_head",
            "start_date",
            "end_date",
            "status",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        department = attrs.get("department") or getattr(self.instance, "department", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)
        if department and user:
            self.validate_user_in_company(user, department.company, field_label="Department member")
        return attrs


class TeamSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    company_id = serializers.UUIDField(source="department.company_id", read_only=True)
    company_name = serializers.CharField(source="department.company.name", read_only=True)
    team_leader = serializers.UUIDField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text="Optional User UUID to assign as the Team Leader.",
    )
    team_leader_id = serializers.SerializerMethodField()
    team_leader_name = serializers.SerializerMethodField()
    active_members_count = serializers.SerializerMethodField()
    mapped_projects_count = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = [
            "id",
            "department",
            "department_name",
            "company_id",
            "company_name",
            "name",
            "code",
            "status",
            "team_leader",
            "team_leader_id",
            "team_leader_name",
            "active_members_count",
            "mapped_projects_count",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        department = attrs.get("department") or getattr(self.instance, "department", None)
        leader_id = attrs.get("team_leader")
        if department and leader_id:
            leader_user = User.objects.filter(id=leader_id).first()
            if not leader_user:
                raise serializers.ValidationError({"team_leader": "Selected team leader user does not exist."})
            self.validate_user_in_company(leader_user, department.company, field_label="Team leader")
        return attrs

    def _sync_team_leader(self, team: Team, leader_user_id):
        if leader_user_id is None:
            return
        TeamMembership.objects.filter(
            team=team,
            status=TeamMembership.Status.ACTIVE,
            is_leader=True,
        ).exclude(user_id=leader_user_id).update(is_leader=False)
        if leader_user_id:
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
            self._sync_team_leader(team, leader_id or "")
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

    def get_active_members_count(self, obj):
        return sum(1 for m in obj.memberships.all() if m.status == TeamMembership.Status.ACTIVE)

    def get_mapped_projects_count(self, obj):
        return obj.project_teams.count()


class TeamMembershipSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    team_name = serializers.CharField(source="team.name", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = TeamMembership
        fields = [
            "id",
            "team",
            "team_name",
            "user",
            "user_full_name",
            "is_leader",
            "joined_at",
            "left_at",
            "status",
        ]
        read_only_fields = ["id", "joined_at"]

    def validate(self, attrs):
        team = attrs.get("team") or getattr(self.instance, "team", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)
        if team and user:
            self.validate_user_in_company(user, team.department.company, field_label="Team member")
        return attrs


class ReportingRelationshipSerializer(CompanyScopedValidationMixin, serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    manager_name = serializers.CharField(source="manager.full_name", read_only=True)

    class Meta:
        model = ReportingRelationship
        fields = [
            "id",
            "company",
            "employee",
            "employee_name",
            "manager",
            "manager_name",
            "start_date",
            "end_date",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        company = attrs.get("company") or getattr(self.instance, "company", None)
        employee = attrs.get("employee") or getattr(self.instance, "employee", None)
        manager = attrs.get("manager") or getattr(self.instance, "manager", None)

        if employee and manager and employee.id == manager.id:
            raise serializers.ValidationError("An employee cannot report to themselves (DB Design §4.2).")
        if company and employee:
            self.validate_user_in_company(employee, company, field_label="Employee")
        if company and manager:
            self.validate_user_in_company(manager, company, field_label="Manager")

        # Cycle check in reporting hierarchy (DB Design §4.2)
        if company and employee and manager:
            visited = {employee.id}
            queue = [manager.id]
            while queue:
                curr = queue.pop(0)
                if curr in visited:
                    raise serializers.ValidationError("Circular reporting relationship detected (DB Design §4.2).")
                visited.add(curr)
                upper_managers = ReportingRelationship.objects.filter(
                    company=company, employee_id=curr, end_date__isnull=True
                ).values_list("manager_id", flat=True)
                queue.extend(upper_managers)
        return attrs
