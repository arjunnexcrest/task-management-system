import django_filters
from .models import TaskReview


class TaskReviewFilterSet(django_filters.FilterSet):
    company = django_filters.UUIDFilter(field_name="task__project__company_id")
    project = django_filters.UUIDFilter(field_name="task__project_id")
    task = django_filters.UUIDFilter(field_name="task_id")
    reviewer = django_filters.UUIDFilter(field_name="reviewer_id")
    task_owner = django_filters.UUIDFilter(field_name="task__task_owner_id")
    priority = django_filters.CharFilter(field_name="task__priority")
    decision = django_filters.CharFilter(field_name="decision")
    review_round = django_filters.NumberFilter(field_name="review_round")
    date_from = django_filters.DateFilter(field_name="reviewed_at__date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="reviewed_at__date", lookup_expr="lte")

    class Meta:
        model = TaskReview
        fields = [
            "company",
            "project",
            "task",
            "reviewer",
            "task_owner",
            "priority",
            "decision",
            "review_round",
            "date_from",
            "date_to",
        ]
