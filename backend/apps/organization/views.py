from datetime import date
from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.common.permissions import (
    DepartmentManagementPermission,
    DepartmentMembershipPermission,
    TeamManagementPermission,
    TeamMembershipPermission,
    UserManagementPermission,
)
from apps.common.selectors import scope_queryset_by_company
from .models import (
    Department,
    DepartmentMembership,
    ReportingRelationship,
    Team,
    TeamMembership,
)
from .serializers import (
    DepartmentMembershipSerializer,
    DepartmentSerializer,
    ReportingRelationshipSerializer,
    TeamMembershipSerializer,
    TeamSerializer,
)


class DepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer
    permission_classes = [DepartmentManagementPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["company", "status"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        qs = (
            Department.objects.select_related("company")
            .prefetch_related("memberships__user", "teams", "project_departments")
            .all()
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="company_id")

    @action(detail=True, methods=["post"])
    def assign_head(self, request, pk=None):
        """
        Assigns a Department Head to this department (SRS §5.1).
        """
        department = self.get_object()
        head_id = request.data.get("department_head") or request.data.get("user")
        serializer = self.get_serializer(
            department,
            data={"department_head": head_id},
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        dept = serializer.save()
        return Response(self.get_serializer(dept).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def transfer_employee(self, request, pk=None):
        """
        Transfers an employee into this department while preserving previous department
        membership history with status='Transferred' and end_date=today (SRS §3.2, §5.1 & DB Design §4.2).
        """
        target_dept = self.get_object()
        user_id = request.data.get("user")
        is_head = bool(request.data.get("is_head", False))
        if not user_id:
            return Response({"error": "user UUID is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Mark active memberships in other departments of the same company as Transferred
        DepartmentMembership.objects.filter(
            department__company=target_dept.company,
            user_id=user_id,
            status=DepartmentMembership.Status.ACTIVE,
        ).exclude(department=target_dept).update(
            status=DepartmentMembership.Status.TRANSFERRED,
            end_date=date.today(),
            is_head=False,
        )

        membership, created = DepartmentMembership.objects.update_or_create(
            department=target_dept,
            user_id=user_id,
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
    permission_classes = [DepartmentMembershipPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["department", "user", "is_head", "status"]

    def get_queryset(self):
        qs = DepartmentMembership.objects.select_related("department", "department__company", "user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="department__company_id")


class TeamViewSet(viewsets.ModelViewSet):
    serializer_class = TeamSerializer
    permission_classes = [TeamManagementPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["department", "status"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        qs = (
            Team.objects.select_related("department", "department__company")
            .prefetch_related("memberships__user", "project_teams")
            .all()
        )
        return scope_queryset_by_company(qs, self.request, company_lookup="department__company_id")

    @action(detail=True, methods=["post"])
    def assign_leader(self, request, pk=None):
        """
        Assigns a Team Leader to this team (SRS §5.2).
        """
        team = self.get_object()
        leader_id = request.data.get("team_leader") or request.data.get("user")
        serializer = self.get_serializer(
            team,
            data={"team_leader": leader_id},
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()
        return Response(self.get_serializer(updated).data)


class TeamMembershipViewSet(viewsets.ModelViewSet):
    serializer_class = TeamMembershipSerializer
    permission_classes = [TeamMembershipPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["team", "user", "is_leader", "status"]

    def get_queryset(self):
        qs = TeamMembership.objects.select_related("team", "team__department__company", "user").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="team__department__company_id")


class ReportingRelationshipViewSet(viewsets.ModelViewSet):
    serializer_class = ReportingRelationshipSerializer
    permission_classes = [UserManagementPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["company", "employee", "manager"]

    def get_queryset(self):
        qs = ReportingRelationship.objects.select_related("company", "employee", "manager").all()
        return scope_queryset_by_company(qs, self.request, company_lookup="company_id")
