from django.db.models import Q
import django_filters
from .models import ProgressUpdate, TimeEntry


class ProgressUpdateFilterSet(django_filters.FilterSet):
    project = django_filters.UUIDFilter(field_name="task__project_id")
    department = django_filters.UUIDFilter(field_name="task__department_id")
    team = django_filters.UUIDFilter(field_name="task__team_id")
    date_from = django_filters.DateFilter(field_name="created_at__date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="created_at__date", lookup_expr="lte")
    has_blockers = django_filters.BooleanFilter(method="filter_has_blockers")

    class Meta:
        model = ProgressUpdate
        fields = [
            "task",
            "author",
            "project",
            "department",
            "team",
            "date_from",
            "date_to",
            "has_blockers",
        ]

    def filter_has_blockers(self, queryset, name, value):
        if value is True:
            return queryset.filter(blockers__isnull=False).exclude(Q(blockers=""))
        if value is False:
            return queryset.filter(Q(blockers__isnull=True) | Q(blockers=""))
        return queryset


class TimeEntryFilterSet(django_filters.FilterSet):
    project = django_filters.UUIDFilter(field_name="task__project_id")
    date_from = django_filters.DateFilter(field_name="created_at__date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="created_at__date", lookup_expr="lte")

    class Meta:
        model = TimeEntry
        fields = [
            "task",
            "user",
            "project",
            "entry_type",
            "approval_status",
            "date_from",
            "date_to",
        ]
