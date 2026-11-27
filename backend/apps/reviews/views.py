from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from apps.common.permissions import IsActiveUser, IsCompanyMember, is_ceo
from apps.tasks.selectors import scope_queryset_by_task
from .models import TaskReview
from .permissions import CanReviewTasks, validate_can_review_task
from .serializers import TaskReviewSerializer


class TaskReviewViewSet(viewsets.ModelViewSet):
    serializer_class = TaskReviewSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "reviewer", "decision", "review_round"]
    ordering_fields = ["reviewed_at", "review_round"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanReviewTasks()]

    def get_queryset(self):
        qs = TaskReview.objects.select_related("task", "task__project", "reviewer").order_by("-reviewed_at")
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        task = serializer.validated_data["task"]
        validate_can_review_task(self.request.user, task)
        if is_ceo(self.request.user):
            reviewer = serializer.validated_data.get("reviewer") or self.request.user
        else:
            supplied = serializer.validated_data.get("reviewer")
            if supplied is not None and supplied.pk != self.request.user.pk:
                raise PermissionDenied("You cannot submit a review on behalf of another user.")
            reviewer = self.request.user
        serializer.save(reviewer=reviewer)

    def perform_update(self, serializer):
        task = serializer.validated_data.get("task") or serializer.instance.task
        validate_can_review_task(self.request.user, task)
        if not is_ceo(self.request.user):
            supplied = serializer.validated_data.get("reviewer")
            if supplied is not None and supplied.pk != self.request.user.pk:
                raise PermissionDenied("You cannot change the reviewer identity.")
        serializer.save()
