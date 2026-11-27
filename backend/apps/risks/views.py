from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from apps.common.permissions import IsActiveUser, IsCompanyMember, is_ceo
from apps.projects.selectors import get_authorized_project_ids, scope_queryset_by_project
from apps.tasks.permissions import CanManageTasks, user_is_project_manager_or_management, user_is_task_manager
from apps.tasks.selectors import get_authorized_task_ids, scope_queryset_by_task
from .models import Blocker, Issue, Risk
from .serializers import BlockerSerializer, IssueSerializer, RiskSerializer


class BlockerViewSet(viewsets.ModelViewSet):
    serializer_class = BlockerSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["task", "resolution_status", "impact", "responsible_person"]
    ordering_fields = ["reported_at"]

    def get_permissions(self):
        if self.action == "destroy":
            return [IsAuthenticated(), IsActiveUser(), CanManageTasks()]
        return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]

    def get_queryset(self):
        qs = Blocker.objects.select_related(
            "task", "task__project", "responsible_person", "reported_by"
        ).order_by("-reported_at")
        return scope_queryset_by_task(
            qs, self.request, task_lookup="task_id", company_lookup="task__project__company_id"
        )

    def perform_create(self, serializer):
        task = serializer.validated_data["task"]
        if not is_ceo(self.request.user):
            allowed = get_authorized_task_ids(self.request.user) or set()
            if task.id not in allowed:
                raise PermissionDenied("You cannot report blockers on tasks outside your authorized projects.")
        is_mgr = user_is_task_manager(self.request.user, task)
        supplied = serializer.validated_data.get("reported_by")
        if not is_mgr and supplied is not None and supplied.pk != self.request.user.pk:
            raise PermissionDenied("You cannot report blockers on behalf of another user.")
        serializer.save(reported_by=supplied if (is_mgr and supplied is not None) else self.request.user)

    def perform_update(self, serializer):
        instance = serializer.instance
        user = self.request.user
        if not (
            user_is_task_manager(user, instance.task)
            or instance.reported_by_id == user.id
            or instance.responsible_person_id == user.id
        ):
            raise PermissionDenied("Only the reporter, responsible person, or a manager can update this blocker.")
        serializer.save()


class RiskViewSet(viewsets.ModelViewSet):
    serializer_class = RiskSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["project", "status", "severity", "probability", "impact", "owner"]
    search_fields = ["title", "description"]
    ordering_fields = ["due_date", "title"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageTasks()]

    def get_queryset(self):
        qs = Risk.objects.select_related("project", "owner").all()
        return scope_queryset_by_project(
            qs, self.request, project_lookup="project_id", company_lookup="project__company_id"
        )

    def perform_create(self, serializer):
        project = serializer.validated_data["project"]
        if not user_is_project_manager_or_management(self.request.user, project):
            raise PermissionDenied("Only managers can register project risks.")
        if not is_ceo(self.request.user):
            allowed = get_authorized_project_ids(self.request.user) or set()
            if project.id not in allowed:
                raise PermissionDenied("You do not have access to this project.")
        serializer.save()


class IssueViewSet(viewsets.ModelViewSet):
    serializer_class = IssueSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["project", "task", "status", "priority", "owner"]
    search_fields = ["title", "description"]
    ordering_fields = ["reported_at", "due_date"]

    def get_permissions(self):
        if self.action == "destroy":
            return [IsAuthenticated(), IsActiveUser(), CanManageTasks()]
        return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]

    def get_queryset(self):
        qs = Issue.objects.select_related("project", "task", "owner", "reported_by").order_by("-reported_at")
        return scope_queryset_by_project(
            qs, self.request, project_lookup="project_id", company_lookup="project__company_id"
        )

    def perform_create(self, serializer):
        project = serializer.validated_data["project"]
        if not is_ceo(self.request.user):
            allowed = get_authorized_project_ids(self.request.user) or set()
            if project.id not in allowed:
                raise PermissionDenied("You cannot report issues on projects you are not authorized to access.")
        is_mgr = user_is_project_manager_or_management(self.request.user, project)
        supplied = serializer.validated_data.get("reported_by")
        if not is_mgr and supplied is not None and supplied.pk != self.request.user.pk:
            raise PermissionDenied("You cannot report issues on behalf of another user.")
        serializer.save(reported_by=supplied if (is_mgr and supplied is not None) else self.request.user)

    def perform_update(self, serializer):
        instance = serializer.instance
        user = self.request.user
        if not (
            user_is_project_manager_or_management(user, instance.project)
            or instance.reported_by_id == user.id
            or instance.owner_id == user.id
        ):
            raise PermissionDenied("Only the reporter, issue owner, or a manager can update this issue.")
        serializer.save()
