import django_filters
from django.db.models import Q
from .models import Task, TaskAssignment


class TaskFilterSet(django_filters.FilterSet):
    """
    Implements the exact Task List filter contract specified in DB Design §8:
    'Task list: filter by active company, authorized project scope, status, priority,
    department, team, owner, assignee and due-date range; paginate and sort in the database.'
    """

    company = django_filters.UUIDFilter(field_name="project__company_id")
    project = django_filters.UUIDFilter(field_name="project_id")
    parent_task = django_filters.UUIDFilter(field_name="parent_task_id")
    is_subtask = django_filters.BooleanFilter(method="filter_is_subtask")
    phase = django_filters.UUIDFilter(field_name="phase_id")
    department = django_filters.UUIDFilter(field_name="department_id")
    team = django_filters.UUIDFilter(field_name="team_id")
    owner = django_filters.UUIDFilter(field_name="task_owner_id")
    task_owner = django_filters.UUIDFilter(field_name="task_owner_id")
    reviewer = django_filters.UUIDFilter(field_name="reviewer_id")
    assignee = django_filters.UUIDFilter(method="filter_assignee")
    assigned_team = django_filters.UUIDFilter(method="filter_assigned_team")
    tag = django_filters.CharFilter(field_name="tag_assignments__tag__name", lookup_expr="iexact", distinct=True)
    due_date_from = django_filters.DateFilter(field_name="due_date", lookup_expr="gte")
    due_date_to = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")
    start_date_from = django_filters.DateFilter(field_name="start_date", lookup_expr="gte")
    start_date_to = django_filters.DateFilter(field_name="start_date", lookup_expr="lte")

    class Meta:
        model = Task
        fields = [
            "company",
            "project",
            "parent_task",
            "is_subtask",
            "phase",
            "department",
            "team",
            "owner",
            "task_owner",
            "reviewer",
            "assignee",
            "assigned_team",
            "priority",
            "status",
            "progress_method",
            "approval_required",
            "tag",
            "due_date_from",
            "due_date_to",
            "start_date_from",
            "start_date_to",
        ]

    def filter_is_subtask(self, queryset, name, value):
        if value is True:
            return queryset.filter(parent_task__isnull=False)
        if value is False:
            return queryset.filter(parent_task__isnull=True)
        return queryset

    def filter_assignee(self, queryset, name, value):
        return queryset.filter(
            Q(assignments__user_id=value, assignments__status__in=["Active", "Acknowledged"])
            | Q(task_owner_id=value)
        ).distinct()

    def filter_assigned_team(self, queryset, name, value):
        return queryset.filter(
            Q(assignments__team_id=value, assignments__status__in=["Active", "Acknowledged"])
            | Q(team_id=value)
        ).distinct()


class TaskAssignmentFilterSet(django_filters.FilterSet):
    company = django_filters.UUIDFilter(field_name="task__project__company_id")
    project = django_filters.UUIDFilter(field_name="task__project_id")

    class Meta:
        model = TaskAssignment
        fields = ["company", "project", "task", "user", "team", "assignment_type", "status"]
