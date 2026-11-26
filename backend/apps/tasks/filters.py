from datetime import timedelta
import django_filters
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from .models import Task, TaskAssignment, TaskDependency
from .services.dependencies import STARTED_STATUSES, TERMINAL_STATUSES


class TaskFilterSet(django_filters.FilterSet):
    """
    Implements the exact Task List & Search filter contract specified in SRS §7.11 & DB Design §8:
    'Task list: filter by active company, authorized project scope, status, priority,
    department, team, owner, assignee, reviewer, overdue status, tags, and date ranges;
    paginate and sort in the database.'
    """

    company = django_filters.UUIDFilter(field_name="project__company_id")
    project = django_filters.UUIDFilter(field_name="project_id")
    parent_task = django_filters.UUIDFilter(field_name="parent_task_id")
    is_subtask = django_filters.BooleanFilter(method="filter_is_subtask")
    overdue = django_filters.BooleanFilter(method="filter_overdue")
    due_status = django_filters.CharFilter(method="filter_due_status")
    dependency_blocked = django_filters.BooleanFilter(method="filter_dependency_blocked")
    has_dependencies = django_filters.BooleanFilter(method="filter_has_dependencies")
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
    created_date_from = django_filters.DateFilter(field_name="created_at__date", lookup_expr="gte")
    created_date_to = django_filters.DateFilter(field_name="created_at__date", lookup_expr="lte")
    completed_date_from = django_filters.DateFilter(field_name="completed_at__date", lookup_expr="gte")
    completed_date_to = django_filters.DateFilter(field_name="completed_at__date", lookup_expr="lte")

    class Meta:
        model = Task
        fields = [
            "company",
            "project",
            "parent_task",
            "is_subtask",
            "overdue",
            "due_status",
            "dependency_blocked",
            "has_dependencies",
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
            "created_date_from",
            "created_date_to",
            "completed_date_from",
            "completed_date_to",
        ]

    def filter_is_subtask(self, queryset, name, value):
        if value is True:
            return queryset.filter(parent_task__isnull=False)
        if value is False:
            return queryset.filter(parent_task__isnull=True)
        return queryset

    def filter_overdue(self, queryset, name, value):
        today = timezone.now().date()
        overdue_q = Q(due_date__lt=today) & ~Q(status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED])
        if value is True:
            return queryset.filter(overdue_q)
        if value is False:
            return queryset.exclude(overdue_q)
        return queryset

    def filter_due_status(self, queryset, name, value):
        val = (value or "").strip().lower()
        today = timezone.now().date()
        open_q = ~Q(status__in=list(TERMINAL_STATUSES))
        if val == "overdue":
            return queryset.filter(open_q & Q(due_date__lt=today))
        if val == "due today":
            return queryset.filter(open_q & Q(due_date=today))
        if val == "due soon":
            return queryset.filter(
                open_q & Q(due_date__gt=today) & Q(due_date__lte=today + timedelta(days=3))
            )
        if val == "on track":
            return queryset.filter(open_q & Q(due_date__gt=today + timedelta(days=3)))
        if val == "completed":
            return queryset.filter(status__in=list(TERMINAL_STATUSES))
        if val == "no due date":
            return queryset.filter(open_q & Q(due_date__isnull=True))
        return queryset

    def filter_dependency_blocked(self, queryset, name, value):
        started_list = list(STARTED_STATUSES)
        terminal_list = list(TERMINAL_STATUSES)

        unsatisfied_start_subq = TaskDependency.objects.filter(
            task_id=OuterRef("pk")
        ).filter(
            (
                Q(dependency_type=TaskDependency.DependencyType.FINISH_TO_START)
                & ~Q(depends_on_task__status=Task.Status.COMPLETED)
            )
            | (
                Q(dependency_type=TaskDependency.DependencyType.START_TO_START)
                & ~Q(depends_on_task__status__in=started_list)
            )
        )
        unsatisfied_complete_subq = TaskDependency.objects.filter(
            task_id=OuterRef("pk")
        ).filter(
            (
                Q(
                    dependency_type__in=[
                        TaskDependency.DependencyType.FINISH_TO_START,
                        TaskDependency.DependencyType.FINISH_TO_FINISH,
                    ]
                )
                & ~Q(depends_on_task__status=Task.Status.COMPLETED)
            )
            | (
                Q(
                    dependency_type__in=[
                        TaskDependency.DependencyType.START_TO_START,
                        TaskDependency.DependencyType.START_TO_FINISH,
                    ]
                )
                & ~Q(depends_on_task__status__in=started_list)
            )
        )

        annotated = queryset.annotate(
            _has_unsat_start=Exists(unsatisfied_start_subq),
            _has_unsat_complete=Exists(unsatisfied_complete_subq),
        )
        blocked_q = (
            (~Q(status__in=started_list) & ~Q(status__in=terminal_list) & Q(_has_unsat_start=True))
            | (Q(status__in=started_list) & ~Q(status__in=terminal_list) & Q(_has_unsat_complete=True))
        )
        if value is True:
            return annotated.filter(blocked_q)
        if value is False:
            return annotated.exclude(blocked_q)
        return queryset

    def filter_has_dependencies(self, queryset, name, value):
        if value is True:
            return queryset.filter(
                Q(dependencies__isnull=False) | Q(dependent_tasks__isnull=False)
            ).distinct()
        if value is False:
            return queryset.filter(
                dependencies__isnull=True, dependent_tasks__isnull=True
            ).distinct()
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
