from rest_framework import serializers
from .models import Blocker, Issue, Risk


class BlockerSerializer(serializers.ModelSerializer):
    task_title = serializers.CharField(source="task.title", read_only=True)
    responsible_person_name = serializers.CharField(source="responsible_person.full_name", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.full_name", read_only=True)

    class Meta:
        model = Blocker
        fields = [
            "id",
            "task",
            "task_title",
            "description",
            "category",
            "impact",
            "responsible_person",
            "responsible_person_name",
            "reported_by",
            "reported_by_name",
            "reported_at",
            "resolution_status",
            "resolved_at",
        ]
        read_only_fields = ["id", "reported_at"]
        extra_kwargs = {"reported_by": {"required": False}}


class RiskSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.name", read_only=True)
    owner_name = serializers.CharField(source="owner.full_name", read_only=True, default=None)

    class Meta:
        model = Risk
        fields = [
            "id",
            "project",
            "project_name",
            "title",
            "description",
            "probability",
            "impact",
            "severity",
            "owner",
            "owner_name",
            "mitigation_plan",
            "due_date",
            "status",
        ]
        read_only_fields = ["id"]


class IssueSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.name", read_only=True)
    task_title = serializers.CharField(source="task.title", read_only=True, default=None)
    owner_name = serializers.CharField(source="owner.full_name", read_only=True, default=None)
    reported_by_name = serializers.CharField(source="reported_by.full_name", read_only=True)

    class Meta:
        model = Issue
        fields = [
            "id",
            "project",
            "project_name",
            "task",
            "task_title",
            "title",
            "description",
            "priority",
            "owner",
            "owner_name",
            "reported_by",
            "reported_by_name",
            "reported_at",
            "due_date",
            "resolution_notes",
            "status",
        ]
        read_only_fields = ["id", "reported_at"]
        extra_kwargs = {"reported_by": {"required": False}}

    def validate(self, attrs):
        project = attrs.get("project") or getattr(self.instance, "project", None)
        task = attrs.get("task") if "task" in attrs else getattr(self.instance, "task", None)
        if project and task and task.project_id != project.id:
            raise serializers.ValidationError(
                {"task": "Linked task must belong to the same project as the issue (DB Design §4.6)."}
            )
        return attrs
