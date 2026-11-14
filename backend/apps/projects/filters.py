import django_filters
from .models import Project, ProjectMilestone, ProjectPhase


class ProjectFilterSet(django_filters.FilterSet):
    company = django_filters.UUIDFilter(field_name="company_id")
    project_manager = django_filters.UUIDFilter(field_name="project_manager_id")
    department = django_filters.UUIDFilter(field_name="project_departments__department_id", distinct=True)
    team = django_filters.UUIDFilter(field_name="project_teams__team_id", distinct=True)
    member = django_filters.UUIDFilter(field_name="members__user_id", distinct=True)
    start_date_from = django_filters.DateFilter(field_name="start_date", lookup_expr="gte")
    start_date_to = django_filters.DateFilter(field_name="start_date", lookup_expr="lte")
    due_date_from = django_filters.DateFilter(field_name="planned_completion_date", lookup_expr="gte")
    due_date_to = django_filters.DateFilter(field_name="planned_completion_date", lookup_expr="lte")

    class Meta:
        model = Project
        fields = [
            "company",
            "status",
            "priority",
            "project_manager",
            "department",
            "team",
            "member",
        ]


class ProjectPhaseFilterSet(django_filters.FilterSet):
    project = django_filters.UUIDFilter(field_name="project_id")
    company = django_filters.UUIDFilter(field_name="project__company_id")

    class Meta:
        model = ProjectPhase
        fields = ["project", "company", "status", "owner"]


class ProjectMilestoneFilterSet(django_filters.FilterSet):
    project = django_filters.UUIDFilter(field_name="project_id")
    company = django_filters.UUIDFilter(field_name="project__company_id")
    target_date_from = django_filters.DateFilter(field_name="target_date", lookup_expr="gte")
    target_date_to = django_filters.DateFilter(field_name="target_date", lookup_expr="lte")

    class Meta:
        model = ProjectMilestone
        fields = ["project", "company", "status", "owner", "approval_required"]
