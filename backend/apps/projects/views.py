from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import User
from apps.common.permissions import (
    IsActiveUser,
    IsCompanyMember,
    get_active_membership,
    get_user_company_ids,
    is_ceo,
)
from .filters import ProjectFilterSet, ProjectMilestoneFilterSet, ProjectPhaseFilterSet
from .models import (
    MilestoneDependency,
    MilestoneTask,
    Project,
    ProjectDepartment,
    ProjectMember,
    ProjectMilestone,
    ProjectPhase,
    ProjectTeam,
)
from .permissions import CanManageProjects
from .selectors import (
    get_authorized_project_ids,
    get_project_detail_queryset,
    get_project_list_queryset,
    scope_queryset_by_project,
)
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
    get_user_project_participations,
    map_project_departments_service,
    map_project_teams_service,
    recalculate_project_progress_service,
    sync_project_team_members_service,
    update_project_member_participation_service,
)


class ProjectViewSet(viewsets.ModelViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ProjectFilterSet
    search_fields = ["name", "code", "description", "objectives", "deliverables"]
    ordering_fields = ["start_date", "planned_completion_date", "created_at", "priority", "progress", "name"]

    READ_ACTIONS = (
        "list",
        "retrieve",
        "assignment_hierarchy",
        "participation",
        "user_participations",
        "tasks",
    )

    DETAIL_ACTIONS = (
        "retrieve",
        "close_project",
        "archive",
        "recalculate_progress",
        "assign_manager",
        "map_departments",
        "map_teams",
        "sync_team_members",
        "assign_members",
        "assignment_hierarchy",
        "participation",
    )

    def get_permissions(self):
        if self.action in self.READ_ACTIONS:
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

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
        Maps participating teams under participating departments, coordinates Team Leaders,
        and optionally enrolls all active team members into ProjectMember participation.
        """
        project = self.get_object()
        team_ids = request.data.get("team_ids", [])
        replace = bool(request.data.get("replace", True))
        auto_map_departments = bool(request.data.get("auto_map_departments", True))
        auto_add_leaders = bool(request.data.get("auto_add_leaders", True))
        auto_add_team_members = bool(request.data.get("auto_add_team_members", False))
        try:
            updated = map_project_teams_service(
                project=project,
                team_ids=team_ids,
                replace=replace,
                auto_map_departments=auto_map_departments,
                auto_add_leaders=auto_add_leaders,
                auto_add_team_members=auto_add_team_members,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_project_detail_queryset(request).get(pk=updated.pk)
        return Response(ProjectDetailSerializer(refreshed).data)

    @action(detail=True, methods=["post"])
    def sync_team_members(self, request, pk=None):
        """
        SRS §5.2 & §6.3 Step 6:
        Enrolls all active members of mapped participating teams (or a specific team_id)
        into ProjectMember(status='Active') so the team roster has full project participation.
        """
        project = self.get_object()
        team_id = request.data.get("team_id") or request.data.get("team")
        team_ids = request.data.get("team_ids")
        try:
            sync_result = sync_project_team_members_service(
                project=project,
                team_id=team_id,
                team_ids=team_ids,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        refreshed = get_project_detail_queryset(request).get(pk=project.pk)
        data = ProjectDetailSerializer(refreshed).data
        data["sync_result"] = sync_result
        return Response(data)

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
        Returns the full SRS §6.3 Project Assignment Hierarchy & Participation Matrix:
        Company -> Project Manager -> Participating Departments & Heads -> Participating Teams & Leaders -> Members.
        """
        project = self.get_object()
        return Response(get_project_assignment_hierarchy(project=project))

    @action(detail=True, methods=["get"])
    def participation(self, request, pk=None):
        """
        Alias endpoint for Project Participation & Team Roster details (SRS §5.2, §6.3 & §25.C Screen 21).
        """
        project = self.get_object()
        return Response(get_project_assignment_hierarchy(project=project))

    @action(detail=False, methods=["get"])
    def user_participations(self, request):
        """
        SRS §1.3, §3.3, §4.3, §19 & §27.1:
        Returns authorized projects in which the specified (or authenticated) employee participates.
        Strictly enforces company- and project-level isolation (PERM-04).
        """
        user_id = request.query_params.get("user") or str(request.user.id)
        company_id = request.query_params.get("company")

        if company_id and not is_ceo(request.user):
            if get_active_membership(request.user, company_id=company_id) is None:
                return Response(
                    {"detail": "You are not an active member of this company."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        if not is_ceo(request.user) and str(user_id) != str(request.user.id):
            requester_cids = get_user_company_ids(request.user)
            target_user = User.objects.filter(pk=user_id).first()
            if target_user is None:
                return Response(
                    {"detail": "User not found within your authorized companies."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            target_cids = get_user_company_ids(target_user)
            if not (requester_cids & target_cids):
                return Response(
                    {"detail": "You are not authorized to inspect participations for a user outside your company."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        participations = get_user_project_participations(user_id=user_id, company_id=company_id)
        if not is_ceo(request.user):
            allowed_pids = {str(pid) for pid in (get_authorized_project_ids(request.user) or set())}
            participations = [p for p in participations if str(p.get("project_id")) in allowed_pids]
        return Response(participations)

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
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "user", "status"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

    def get_queryset(self):
        qs = ProjectMember.objects.select_related("project", "project__company", "user").all()
        return scope_queryset_by_project(
            qs, self.request, project_lookup="project_id", company_lookup="project__company_id"
        )

    def destroy(self, request, *args, **kwargs):
        """
        Preserves membership history by marking status='Removed' and setting left_at
        unless ?hard=true is requested (DB Design §1 & §4.3).
        Protects the accountable Project Manager of an Active project.
        """
        instance = self.get_object()
        if (
            instance.project.project_manager_id == instance.user_id
            and instance.project.status == Project.Status.ACTIVE
        ):
            return Response(
                {
                    "error": "Cannot remove the accountable Project Manager of an Active project. Reassign the Project Manager first (SRS §1.4 #2 & DB Design §4.3)."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if request.query_params.get("hard", "").lower() in ("true", "1", "yes"):
            instance.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        try:
            updated = update_project_member_participation_service(
                member=instance,
                status_val=ProjectMember.Status.REMOVED,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProjectMemberSerializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        """
        Reactivates a Removed or Inactive ProjectMember record (status='Active', left_at=None).
        """
        instance = self.get_object()
        role = request.data.get("role_in_project")
        try:
            updated = update_project_member_participation_service(
                member=instance,
                role_in_project=role,
                status_val=ProjectMember.Status.ACTIVE,
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") else str(exc)
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProjectMemberSerializer(updated).data, status=status.HTTP_200_OK)


class ProjectDepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectDepartmentSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "department"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

    def get_queryset(self):
        qs = ProjectDepartment.objects.select_related("project", "department").all()
        return scope_queryset_by_project(
            qs, self.request, project_lookup="project_id", company_lookup="project__company_id"
        )


class ProjectTeamViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectTeamSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "team"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

    def get_queryset(self):
        qs = ProjectTeam.objects.select_related("project", "team", "team__department").all()
        return scope_queryset_by_project(
            qs, self.request, project_lookup="project_id", company_lookup="project__company_id"
        )


class ProjectPhaseViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectPhaseSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ProjectPhaseFilterSet
    ordering_fields = ["start_date", "end_date", "progress", "name"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

    def get_queryset(self):
        qs = ProjectPhase.objects.select_related("project", "owner").all()
        return scope_queryset_by_project(
            qs, self.request, project_lookup="project_id", company_lookup="project__company_id"
        )


class ProjectMilestoneViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectMilestoneSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ProjectMilestoneFilterSet
    ordering_fields = ["target_date", "progress", "title"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

    def get_queryset(self):
        qs = (
            ProjectMilestone.objects.select_related("project", "owner")
            .prefetch_related("milestone_tasks__task", "dependencies__depends_on_milestone")
            .all()
        )
        return scope_queryset_by_project(
            qs, self.request, project_lookup="project_id", company_lookup="project__company_id"
        )


class MilestoneTaskViewSet(viewsets.ModelViewSet):
    serializer_class = MilestoneTaskSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["milestone", "task"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

    def get_queryset(self):
        qs = MilestoneTask.objects.select_related("milestone__project", "task").all()
        return scope_queryset_by_project(
            qs,
            self.request,
            project_lookup="milestone__project_id",
            company_lookup="milestone__project__company_id",
        )


class MilestoneDependencyViewSet(viewsets.ModelViewSet):
    serializer_class = MilestoneDependencySerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["milestone", "depends_on_milestone"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsActiveUser(), IsCompanyMember()]
        return [IsAuthenticated(), IsActiveUser(), CanManageProjects()]

    def get_queryset(self):
        qs = MilestoneDependency.objects.select_related("milestone__project", "depends_on_milestone").all()
        return scope_queryset_by_project(
            qs,
            self.request,
            project_lookup="milestone__project_id",
            company_lookup="milestone__project__company_id",
        )
