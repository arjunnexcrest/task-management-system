from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from apps.accounts.models import User
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.common.selectors import scope_queryset_by_company
from .models import TaskReview
from .serializers import TaskReviewSerializer


class TaskReviewViewSet(viewsets.ModelViewSet):
    serializer_class = TaskReviewSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "reviewer", "decision", "review_round"]
    ordering_fields = ["reviewed_at", "review_round"]

    def get_queryset(self):
        qs = TaskReview.objects.select_related("task", "task__project", "reviewer").order_by("-reviewed_at")
        return scope_queryset_by_company(qs, self.request, company_lookup="task__project__company_id")

    def perform_create(self, serializer):
        if "reviewer" not in serializer.validated_data:
            user = (
                self.request.user
                if self.request and getattr(self.request, "user", None) and self.request.user.is_authenticated
                else User.objects.first()
            )
            serializer.save(reviewer=user)
        else:
            serializer.save()
