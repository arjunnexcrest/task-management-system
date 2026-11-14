from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.common.selectors import scope_queryset_by_company
from .filters import ProjectFilterSet, ProjectMilestoneFilterSet, ProjectPhaseFilterSet
from .models import (
    MilestoneDependency,
    MilestoneTask,
    ProjectDepartment,
    ProjectMember,
    ProjectMilestone,
    ProjectPhase,
    ProjectTeam,
)
from .selectors import get_project_detail_queryset, get_project_list_queryset
from .serializers import (
    MilestoneDependencySerializer,
    MilestoneTaskSerializer,
    ProjectDepartmentSerializer,
    ProjectDetailSerializer,
    ProjectMemberSerializer,
    ProjectMilestoneSerializer,
    ProjectPhaseSerializer,
    ProjectSerializer,
    ProjectTeamSerializer,
)
from .services import (
    archive_project_service,
    assign_project_manager_service,
    assign_project_members_service,
    close_project_service,
    delete_project_service,
    get_project_assignment_hierarchy,
    map_project_departments_service,
    map_project_teams_service,
    recalculate_project_progress_service,
)


class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ProjectFilterSet
    search_fields = ["name", "code", "description", "objectives", "deliverables"]
    ordering_fields = ["start_date", "planned_completion_date", "created_at", "priority", "progress", "name"]

    DETAIL_ACTIONS = (
        "retrieve",
        "close_project",
        "archive",
        "recalculate_progress",
        "assign_manager",
        "map_departments",
        "map_teams",
        "assign_members",
        "assignment_hierarchy",
    )

    def get_queryset(self):
        if self.action in self.DETAIL_ACTIONS:
            return get_project_detail_queryset(self.request)
        return get_project_list_queryset(self.request)

    def get_serializer_class(self):
        if self.action in self.DETAIL_ACTIONS:
            return ProjectDetailSerializer
        return ProjectSerializer

    def destroy(self, request, *args, **kwargs):
        project = self.get_object()
        mode = request.query_params.get("mode", "").lower()
        if mode == "archive":
            archived = archive_project_service(project=project)
            return Response(ProjectDetailSerializer(archived).data, status=status.HTTP_200_OK)

        force_param = request.query_params.get("force", "true").lower()
        force = force_param not in ("false", "0", "no")
        try:
            delete_project_service(project=project, force=force)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def assign_manager(self, request, pk=None):
        """
        SRS §2.2 & §6.3 Step 4:
        Assigns or reassigns an accountable Project Manager to the project.
        """
        project = self.get_object()
        manager_id = request.data.get("project_manager") or request.data.get("user")
        auto_activate = bool(request.data.get("auto_activate", False))
        try:
            updated = assign_project_manager_service(
                project=project,
                project_manager_id=manager_id,
                auto_activate=auto_activate,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_project_detail_queryset(request).get(pk=updated.pk)
        return Response(ProjectDetailSerializer(refreshed).data)

    @action(detail=True, methods=["post"])
    def map_departments(self, request, pk=None):
        """
        SRS §2.3, §5.1 & §6.3 Step 5:
        Maps participating departments to the project and coordinates Department Heads.
        """
        project = self.get_object()
        department_ids = request.data.get("department_ids", [])
        replace = bool(request.data.get("replace", True))
        auto_add_heads = bool(request.data.get("auto_add_heads", True))
        try:
            updated = map_project_departments_service(
                project=project,
                department_ids=department_ids,
                replace=replace,
                auto_add_heads=auto_add_heads,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_project_detail_queryset(request).get(pk=updated.pk)
        return Response(ProjectDetailSerializer(refreshed).data)

    @action(detail=True, methods=["post"])
    def map_teams(self, request, pk=None):
        """
        SRS §2.4, §5.2 & §6.3 Step 6:
        Maps participating teams under participating departments and coordinates Team Leaders.
        """
        project = self.get_object()
        team_ids = request.data.get("team_ids", [])
        replace = bool(request.data.get("replace", True))
        auto_map_departments = bool(request.data.get("auto_map_departments", True))
        auto_add_leaders = bool(request.data.get("auto_add_leaders", True))
        try:
            updated = map_project_teams_service(
                project=project,
                team_ids=team_ids,
                replace=replace,
                auto_map_departments=auto_map_departments,
                auto_add_leaders=auto_add_leaders,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_project_detail_queryset(request).get(pk=updated.pk)
        return Response(ProjectDetailSerializer(refreshed).data)

    @action(detail=True, methods=["post"])
    def assign_members(self, request, pk=None):
        """
        SRS §6.1 & §6.3 Step 6:
        Assigns project team members with roles while preserving historical membership records.
        """
        project = self.get_object()
        members = request.data.get("members")
        member_ids = request.data.get("member_ids")
        replace = bool(request.data.get("replace", False))
        try:
            updated = assign_project_members_service(
                project=project,
                members=members,
                member_ids=member_ids,
                replace=replace,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_project_detail_queryset(request).get(pk=updated.pk)
        return Response(ProjectDetailSerializer(refreshed).data)

    @action(detail=True, methods=["get"])
    def assignment_hierarchy(self, request, pk=None):
        """
        Returns the full SRS §6.3 Project Assignment Hierarchy:
        Company -> Project Manager -> Participating Departments & Heads -> Participating Teams & Leaders -> Members.
        """
        project = self.get_object()
        return Response(get_project_assignment_hierarchy(project=project))

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        project = self.get_object()
        archived = archive_project_service(project=project)
        return Response(ProjectDetailSerializer(archived).data)

    @action(detail=True, methods=["post"])
    def close_project(self, request, pk=None):
        project = self.get_object()
        try:
            closed = close_project_service(
                project=project,
                closure_remarks=request.data.get("closure_remarks", ""),
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProjectDetailSerializer(closed).data)

    @action(detail=True, methods=["post"])
    def recalculate_progress(self, request, pk=None):
        project = self.get_object()
        updated = recalculate_project_progress_service(project=project)
        return Response(ProjectDetailSerializer(updated).data)

    @action(detail=True, methods=["get"])
    def tasks(self, request, pk=None):
        """
        Paginated tasks belonging to this project.
        """
        from apps.tasks.selectors import get_task_list_queryset
        from apps.tasks.serializers import TaskSerializer

        project = self.get_object()
        qs = get_task_list_queryset(request).filter(project=project)
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(TaskSerializer(page, many=True).data)
        return Response(TaskSerializer(qs, many=True).data)


class ProjectMemberViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectMemberSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "user", "status"]

    def get_queryset(self):
        qs = ProjectMember.objects.select_related("project", "project__company", "user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="project__company_id")

    def destroy(self, request, *args, **kwargs):
        """
        Preserves membership history by marking status='Removed' and setting left_at
        unless ?hard=true is requested (DB Design §1 & §4.3).
        """
        instance = self.get_object()
        if request.query_params.get("hard", "").lower() in ("true", "1", "yes"):
            instance.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        instance.status = ProjectMember.Status.REMOVED
        instance.left_at = timezone.now()
        instance.save(update_fields=["status", "left_at"])
        return Response(ProjectMemberSerializer(instance).data, status=status.HTTP_200_OK)


class ProjectDepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectDepartmentSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "department"]

    def get_queryset(self):
        qs = ProjectDepartment.objects.select_related("project", "department").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="project__company_id")


class ProjectTeamViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectTeamSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "team"]

    def get_queryset(self):
        qs = ProjectTeam.objects.select_related("project", "team", "team__department").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="project__company_id")


class ProjectPhaseViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectPhaseSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ProjectPhaseFilterSet
    ordering_fields = ["start_date", "end_date", "progress", "name"]

    def get_queryset(self):
        qs = ProjectPhase.objects.select_related("project", "owner").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="project__company_id")


class ProjectMilestoneViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectMilestoneSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ProjectMilestoneFilterSet
    ordering_fields = ["target_date", "progress", "title"]

    def get_queryset(self):
        qs = (
            ProjectMilestone.objects.select_related("project", "owner")
            .prefetch_related(
                "milestone_tasks__task",
                "dependencies__depends_on_milestone",
            )
            .all()
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="project__company_id")


class MilestoneTaskViewSet(viewsets.ModelViewSet):
    serializer_class = MilestoneTaskSerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["milestone", "task"]

    def get_queryset(self):
        qs = MilestoneTask.objects.select_related("milestone", "milestone__project", "task").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="milestone__project__company_id")


class MilestoneDependencyViewSet(viewsets.ModelViewSet):
    serializer_class = MilestoneDependencySerializer
    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["milestone", "depends_on_milestone"]

    def get_queryset(self):
        qs = MilestoneDependency.objects.select_related(
            "milestone", "milestone__project", "depends_on_milestone"
        ).all()
        return scope_queryset_by_company(qs, self.request, company_lookup="milestone__project__company_id")
