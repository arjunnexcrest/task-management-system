from datetime import date
from django.db import transaction
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from apps.common.permissions import (
    IsCompanyMember,
    IsCompanyMemberOrAllowAnyInDev,
)
from apps.common.selectors import scope_queryset_by_company
from .models import (
    Department,
    DepartmentMembership,
    ReportingRelationship,
    Team,
    TeamMembership,
)
from .permissions import (
    CanManageDepartments,
    CanManageReportingRelationships,
    CanManageTeams,
)
from .serializers import (
    AssignDepartmentHeadSerializer,
    AssignTeamLeaderSerializer,
    DepartmentMembershipSerializer,
    DepartmentSerializer,
    ManageTeamMemberSerializer,
    ReportingRelationshipSerializer,
    TeamMembershipSerializer,
    TeamSerializer,
    TransferEmployeeSerializer,
)


class DepartmentViewSet(viewsets.ModelViewSet):
    """
    Department CRUD and Department-Head assignment (SRS §5.1):
    - Endpoints: /api/departments/ and /api/organization/departments/
    - Read (list, retrieve): active company members (scoped to own companies); CEO sees all.
    - Write (create, update, deactivate/delete, assign_head, transfer_employee):
      CEO (all companies) or Company In-Charge (own company only). Other roles get 403.
    - DELETE performs a soft-deactivation (status='Inactive') to preserve history.
    """

    serializer_class = DepartmentSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["company", "status"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at", "status"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsCompanyMember()]
        return [IsAuthenticated(), CanManageDepartments()]

    def get_queryset(self):
        qs = (
            Department.objects.select_related("company")
            .prefetch_related("memberships__user", "teams", "project_departments")
            .all()
            .order_by("name")
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="company_id")

    def perform_destroy(self, instance: Department):
        """Soft-deactivate instead of hard-deleting to preserve membership/project history."""
        instance.deactivate()

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate_department(self, request, pk=None):
        """POST /api/departments/<id>/deactivate/ — Soft-deactivate a department."""
        department = self.get_object()
        department.deactivate()
        return Response(
            {
                "detail": "Department deactivated.",
                "department": self.get_serializer(department).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="activate")
    def activate_department(self, request, pk=None):
        """POST /api/departments/<id>/activate/ — Reactivate a department."""
        department = self.get_object()
        department.activate()
        return Response(
            {
                "detail": "Department activated.",
                "department": self.get_serializer(department).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="assign_head")
    def assign_head(self, request, pk=None):
        """
        Assigns or unassigns a Department Head for this department (SRS §5.1).
        Accepts {"department_head": "<user_uuid>" | null} or {"user": "<user_uuid>" | null}.
        """
        department = self.get_object()
        input_serializer = AssignDepartmentHeadSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        if "department_head" in input_serializer.validated_data:
            head_id = input_serializer.validated_data["department_head"]
        else:
            head_id = input_serializer.validated_data.get("user")

        serializer = self.get_serializer(
            department,
            data={"department_head": head_id},
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        dept = serializer.save()
        refreshed = self.get_queryset().get(pk=dept.pk)
        return Response(self.get_serializer(refreshed).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="transfer_employee")
    @transaction.atomic
    def transfer_employee(self, request, pk=None):
        """
        Transfers an employee into this department while preserving previous department
        membership history with status='Transferred' and end_date=today (SRS §3.2, §5.1 & DB Design §4.2).
        """
        target_dept = self.get_object()
        input_serializer = TransferEmployeeSerializer(
            data=request.data,
            context={"department": target_dept},
        )
        input_serializer.is_valid(raise_exception=True)
        user_obj = input_serializer.validated_data["user"]
        is_head = input_serializer.validated_data["is_head"]

        # Mark active memberships in other departments of the same company as Transferred
        DepartmentMembership.objects.filter(
            department__company=target_dept.company,
            user=user_obj,
            status=DepartmentMembership.Status.ACTIVE,
        ).exclude(department=target_dept).update(
            status=DepartmentMembership.Status.TRANSFERRED,
            end_date=date.today(),
            is_head=False,
        )

        # If transferring in as Department Head, demote any previous head in target_dept
        if is_head:
            DepartmentMembership.objects.filter(
                department=target_dept,
                status=DepartmentMembership.Status.ACTIVE,
                is_head=True,
            ).exclude(user=user_obj).update(is_head=False)

        membership, _ = DepartmentMembership.objects.update_or_create(
            department=target_dept,
            user=user_obj,
            status=DepartmentMembership.Status.ACTIVE,
            defaults={
                "is_head": is_head,
                "start_date": date.today(),
                "end_date": None,
            },
        )
        return Response(DepartmentMembershipSerializer(membership).data, status=status.HTTP_200_OK)


class DepartmentMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentMembershipSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["department", "user", "is_head", "status"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsCompanyMember()]
        return [IsAuthenticated(), CanManageDepartments()]

    def get_queryset(self):
        qs = DepartmentMembership.objects.select_related("department", "department__company", "user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="department__company_id")


class TeamViewSet(viewsets.ModelViewSet):
    """
    Team CRUD and Team-Leader assignment (SRS §5.2):
    - Endpoints: /api/teams/ and /api/organization/teams/
    - Read (list, retrieve): active company members (scoped to own companies); CEO sees all.
    - Write (create, update, deactivate/delete, assign_leader, add_member, remove_member):
      CEO (all companies), Company In-Charge (own company), or Department Head (own department).
    - DELETE performs a soft-deactivation (status='Inactive') to preserve history.
    """

    serializer_class = TeamSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["department", "status", "department__company"]
    search_fields = ["name", "code", "department__name"]
    ordering_fields = ["name", "created_at", "status"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsCompanyMember()]
        return [IsAuthenticated(), CanManageTeams()]

    def get_queryset(self):
        qs = (
            Team.objects.select_related("department", "department__company")
            .prefetch_related("memberships__user", "project_teams")
            .all()
            .order_by("name")
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="department__company_id")

    def perform_destroy(self, instance: Team):
        """Soft-deactivate instead of hard-deleting to preserve team/task history."""
        instance.deactivate()

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate_team(self, request, pk=None):
        """POST /api/teams/<id>/deactivate/ — Soft-deactivate a team."""
        team = self.get_object()
        team.deactivate()
        return Response(
            {
                "detail": "Team deactivated.",
                "team": self.get_serializer(team).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="activate")
    def activate_team(self, request, pk=None):
        """POST /api/teams/<id>/activate/ — Reactivate a team."""
        team = self.get_object()
        team.activate()
        return Response(
            {
                "detail": "Team activated.",
                "team": self.get_serializer(team).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="assign_leader")
    def assign_leader(self, request, pk=None):
        """
        Assigns or unassigns a Team Leader for this team (SRS §5.2).
        Accepts {"team_leader": "<user_uuid>" | null} or {"user": "<user_uuid>" | null}.
        """
        team = self.get_object()
        input_serializer = AssignTeamLeaderSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        if "team_leader" in input_serializer.validated_data:
            leader_id = input_serializer.validated_data["team_leader"]
        else:
            leader_id = input_serializer.validated_data.get("user")

        serializer = self.get_serializer(
            team,
            data={"team_leader": leader_id},
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        refreshed = self.get_queryset().get(pk=updated.pk)
        return Response(self.get_serializer(refreshed).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="add_member")
    @transaction.atomic
    def add_member(self, request, pk=None):
        """
        Adds an active company member to this team (optionally designating as Team Leader).
        """
        team = self.get_object()
        input_serializer = ManageTeamMemberSerializer(
            data=request.data,
            context={"team": team, "require_active": True},
        )
        input_serializer.is_valid(raise_exception=True)
        user_obj = input_serializer.validated_data["user"]
        is_leader = input_serializer.validated_data["is_leader"]

        if is_leader:
            TeamMembership.objects.filter(
                team=team,
                status=TeamMembership.Status.ACTIVE,
                is_leader=True,
            ).exclude(user=user_obj).update(is_leader=False)

        membership = TeamMembership.objects.filter(
            team=team,
            user=user_obj,
            status=TeamMembership.Status.ACTIVE,
        ).first()
        if membership:
            if is_leader and not membership.is_leader:
                membership.is_leader = True
                membership.save(update_fields=["is_leader"])
            created = False
        else:
            membership = TeamMembership.objects.create(
                team=team,
                user=user_obj,
                is_leader=is_leader,
                status=TeamMembership.Status.ACTIVE,
            )
            created = True

        return Response(
            TeamMembershipSerializer(membership).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="remove_member")
    @transaction.atomic
    def remove_member(self, request, pk=None):
        """
        Removes a member from this team by marking membership status='Left' and setting left_at
        (preserves membership history per DB Design §4.2).
        """
        team = self.get_object()
        input_serializer = ManageTeamMemberSerializer(
            data=request.data,
            context={"team": team, "require_active": False},
        )
        input_serializer.is_valid(raise_exception=True)
        user_obj = input_serializer.validated_data["user"]

        membership = TeamMembership.objects.filter(
            team=team,
            user=user_obj,
            status=TeamMembership.Status.ACTIVE,
        ).first()
        if not membership:
            return Response(
                {"detail": "User is not an active member of this team."},
                status=status.HTTP_404_NOT_FOUND,
            )

        membership.status = TeamMembership.Status.LEFT
        membership.is_leader = False
        membership.left_at = timezone.now()
        membership.save(update_fields=["status", "is_leader", "left_at"])
        return Response(TeamMembershipSerializer(membership).data, status=status.HTTP_200_OK)


class TeamMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = TeamMembershipSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["team", "user", "is_leader", "status"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsCompanyMember()]
        return [IsAuthenticated(), CanManageTeams()]

    def get_queryset(self):
        qs = TeamMembership.objects.select_related("team", "team__department__company", "user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="team__department__company_id")

    def perform_destroy(self, instance: TeamMembership):
        instance.status = TeamMembership.Status.LEFT
        instance.is_leader = False
        instance.left_at = timezone.now()
        instance.save(update_fields=["status", "is_leader", "left_at"])


class ReportingRelationshipViewSet(viewsets.ModelViewSet):
    """
    Reporting-Manager Relationship APIs (SRS §5.3 & DB Design §4.2):
    - Endpoints: /api/reporting-relationships/ and /api/organization/reporting-relationships/
    - Read (list, retrieve, hierarchy): active company members (scoped to own companies); CEO sees all.
    - Write (create, update, end, delete): CEO or Company In-Charge (own company).
    - DELETE soft-ends the relationship (sets end_date=today) to preserve history.
    """

    serializer_class = ReportingRelationshipSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["company", "employee", "manager"]
    search_fields = [
        "employee__full_name",
        "employee__email",
        "manager__full_name",
        "manager__email",
    ]
    ordering_fields = ["start_date", "end_date"]

    def get_permissions(self):
        if self.action in ("list", "retrieve", "hierarchy"):
            return [IsAuthenticated(), IsCompanyMember()]
        return [IsAuthenticated(), CanManageReportingRelationships()]

    def get_queryset(self):
        qs = (
            ReportingRelationship.objects.select_related("company", "employee", "manager")
            .all()
            .order_by("-start_date")
        )
        active_only = self.request.query_params.get("active_only")
        if active_only and active_only.lower() in ("true", "1", "yes"):
            qs = qs.filter(end_date__isnull=True)
        return scope_queryset_by_company(qs, self.request, company_lookup="company_id")

    def perform_destroy(self, instance: ReportingRelationship):
        """Soft-end the reporting relationship to preserve history."""
        if instance.end_date is None:
            instance.end_date = date.today()
            instance.save(update_fields=["end_date"])

    @action(detail=True, methods=["post"], url_path="end")
    def end_relationship(self, request, pk=None):
        """POST /api/reporting-relationships/<id>/end/ — End an active reporting relationship."""
        rel = self.get_object()
        raw_end = request.data.get("end_date")
        if raw_end:
            try:
                end_dt = date.fromisoformat(str(raw_end))
            except ValueError:
                return Response(
                    {"end_date": ["Invalid date format. Expected YYYY-MM-DD."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            end_dt = date.today()
        if end_dt < rel.start_date:
            return Response(
                {"end_date": ["End date cannot be earlier than start date."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rel.end_date = end_dt
        rel.save(update_fields=["end_date"])
        return Response(
            {
                "detail": "Reporting relationship ended.",
                "relationship": self.get_serializer(rel).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="hierarchy")
    def hierarchy(self, request):
        """
        GET /api/reporting-relationships/hierarchy/?company=&department=&team=
        Returns active employees in scope and their active reporting-manager edges
        for rendering the Organization Chart (CEO -> employee tree).
        """
        from apps.companies.models import CompanyMembership

        company_id = request.query_params.get("company")
        department_id = request.query_params.get("department")
        team_id = request.query_params.get("team")

        cm_qs = CompanyMembership.objects.filter(
            is_active=True,
            status=CompanyMembership.Status.ACTIVE,
            user__is_active=True,
        ).select_related("user", "company")
        cm_qs = scope_queryset_by_company(cm_qs, request, company_lookup="company_id")

        if department_id:
            dept_user_ids = DepartmentMembership.objects.filter(
                department_id=department_id,
                status=DepartmentMembership.Status.ACTIVE,
            ).values_list("user_id", flat=True)
            cm_qs = cm_qs.filter(user_id__in=dept_user_ids)

        if team_id:
            team_user_ids = TeamMembership.objects.filter(
                team_id=team_id,
                status=TeamMembership.Status.ACTIVE,
            ).values_list("user_id", flat=True)
            cm_qs = cm_qs.filter(user_id__in=team_user_ids)

        memberships = list(cm_qs)
        user_ids = {m.user_id for m in memberships}

        dept_map: dict[str, list[str]] = {}
        for dm in DepartmentMembership.objects.filter(
            user_id__in=user_ids,
            status=DepartmentMembership.Status.ACTIVE,
        ).select_related("department"):
            dept_map.setdefault(str(dm.user_id), []).append(dm.department.name)

        team_map: dict[str, list[str]] = {}
        for tm in TeamMembership.objects.filter(
            user_id__in=user_ids,
            status=TeamMembership.Status.ACTIVE,
        ).select_related("team"):
            team_map.setdefault(str(tm.user_id), []).append(tm.team.name)

        rel_qs = ReportingRelationship.objects.filter(
            end_date__isnull=True,
        ).select_related("company", "employee", "manager")
        rel_qs = scope_queryset_by_company(rel_qs, request, company_lookup="company_id")
        if company_id:
            rel_qs = rel_qs.filter(company_id=company_id)

        active_rels = [
            r for r in rel_qs if r.employee_id in user_ids and r.manager_id in user_ids
        ]

        # Build deduplicated employee nodes (preferring the requested company's role)
        nodes_by_id: dict[str, dict] = {}
        for m in memberships:
            uid = str(m.user_id)
            if uid not in nodes_by_id:
                nodes_by_id[uid] = {
                    "id": uid,
                    "full_name": m.user.full_name,
                    "email": m.user.email,
                    "employee_id": m.user.employee_id,
                    "designation": m.user.designation,
                    "role": m.role,
                    "company_id": str(m.company_id),
                    "company_name": m.company.name,
                    "company_code": m.company.code,
                    "departments": dept_map.get(uid, []),
                    "teams": team_map.get(uid, []),
                    "manager_id": None,
                    "relationship_id": None,
                }

        edges = []
        for r in active_rels:
            emp_id = str(r.employee_id)
            mgr_id = str(r.manager_id)
            if emp_id in nodes_by_id and nodes_by_id[emp_id]["manager_id"] is None:
                nodes_by_id[emp_id]["manager_id"] = mgr_id
                nodes_by_id[emp_id]["relationship_id"] = str(r.id)
            edges.append(
                {
                    "id": str(r.id),
                    "company_id": str(r.company_id),
                    "employee_id": emp_id,
                    "manager_id": mgr_id,
                    "start_date": r.start_date.isoformat(),
                }
            )

        return Response(
            {
                "nodes": list(nodes_by_id.values()),
                "edges": edges,
            },
            status=status.HTTP_200_OK,
        )
