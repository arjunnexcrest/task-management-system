from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Company, CompanyMembership
from .permissions import IsCEO, IsCompanyMember, get_user_company_ids, is_ceo
from .serializers import CompanyMembershipSerializer, CompanySerializer


class CompanyViewSet(viewsets.ModelViewSet):
    """
    Company CRUD at /api/companies/
    - Only the CEO can create, view, update, and delete companies (other roles get 403; unauthenticated gets 401).
    - DELETE performs a soft delete (is_active=False, status='Inactive'), never removing the row.
    - Supports search by name and pagination.
    """

    queryset = Company.objects.prefetch_related("memberships").all().order_by("name")
    serializer_class = CompanySerializer
    permission_classes = [IsAuthenticated, IsCEO]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active", "status", "code"]
    search_fields = ["name", "code", "email", "phone", "address"]
    ordering_fields = ["name", "code", "created_at"]

    def perform_destroy(self, instance: Company):
        instance.soft_delete()


class CompanyMembershipViewSet(viewsets.ModelViewSet):
    """
    Company Membership APIs at /api/memberships/ (and /api/companies/memberships/):
    - CEO can add a user to a company with a role, change role, activate, and deactivate.
    - Non-CEO users can only view memberships in their own active companies (non-members get 403).
    - DELETE soft-deactivates the membership (is_active=False).
    """

    serializer_class = CompanyMembershipSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["company", "user", "role", "is_active", "status"]
    search_fields = ["user__full_name", "user__email", "company__name", "role"]
    ordering_fields = ["joined_at", "role"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), IsCompanyMember()]
        return [IsAuthenticated(), IsCEO()]

    def get_queryset(self):
        qs = CompanyMembership.objects.select_related("company", "user").all().order_by("-joined_at")
        user = self.request.user
        if is_ceo(user):
            return qs
        cids = get_user_company_ids(user)
        if not cids:
            return qs.none()
        return qs.filter(company_id__in=cids)

    def perform_destroy(self, instance: CompanyMembership):
        instance.deactivate()

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate_membership(self, request, pk=None):
        """POST /api/memberships/<id>/deactivate/ — Deactivate membership."""
        membership = self.get_object()
        membership.deactivate()
        return Response(
            {
                "detail": "Membership deactivated.",
                "membership": CompanyMembershipSerializer(membership, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="activate")
    def activate_membership(self, request, pk=None):
        """POST /api/memberships/<id>/activate/ — Reactivate membership."""
        membership = self.get_object()
        membership.activate()
        return Response(
            {
                "detail": "Membership activated.",
                "membership": CompanyMembershipSerializer(membership, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )
