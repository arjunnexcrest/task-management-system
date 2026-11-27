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
    - DELETE performs a soft delete (is_active=False), never removing the row.
    - Supports search by name and pagination.
    """

    queryset = Company.objects.all().order_by("name")
    serializer_class = CompanySerializer
    permission_classes = [IsAuthenticated, IsCEO]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active", "status", "code"]
    search_fields = ["name", "code", "email", "phone", "address"]
    ordering_fields = ["name", "code", "created_at"]

    def get_permissions(self):
        if self.action == "my_companies":
            return [IsAuthenticated(), IsCompanyMember()]
        return [IsAuthenticated(), IsCEO()]

    def perform_destroy(self, instance: Company):
        instance.soft_delete()

    @action(detail=False, methods=["get"], url_path="my-companies")
    def my_companies(self, request):
        """
        GET /api/companies/my-companies/ — Returns active companies the logged-in user belongs to
        (CEO receives all active companies). Non-members receive 403.
        """
        if is_ceo(request.user):
            qs = Company.objects.filter(is_active=True).order_by("name")
        else:
            cids = get_user_company_ids(request.user)
            qs = Company.objects.filter(id__in=cids, is_active=True).order_by("name")
        return Response(
            CompanySerializer(qs, many=True, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get", "post"], url_path="members")
    def members(self, request, pk=None):
        """
        GET /api/companies/<id>/members/ — List memberships for this company (CEO-only).
        POST /api/companies/<id>/members/ — Add an employee to this company with a role (CEO-only).
        """
        company = self.get_object()
        if request.method == "POST":
            payload = dict(request.data)
            payload["company"] = str(company.id)
            serializer = CompanyMembershipSerializer(data=payload, context={"request": request})
            serializer.is_valid(raise_exception=True)
            membership = serializer.save()
            return Response(
                CompanyMembershipSerializer(membership, context={"request": request}).data,
                status=status.HTTP_201_CREATED,
            )

        qs = (
            CompanyMembership.objects.filter(company=company)
            .select_related("company", "user")
            .order_by("-is_active", "-joined_at")
        )
        is_active_param = request.query_params.get("is_active")
        if is_active_param is not None:
            if is_active_param.lower() in ("true", "1", "yes"):
                qs = qs.filter(is_active=True)
            elif is_active_param.lower() in ("false", "0", "no"):
                qs = qs.filter(is_active=False)
        return Response(
            CompanyMembershipSerializer(qs, many=True, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )


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
        if not membership.company.is_active:
            return Response(
                {"detail": "Cannot activate a membership in an inactive company."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not membership.user.is_active:
            return Response(
                {"detail": "Cannot activate a membership for an inactive user."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        membership.activate()
        return Response(
            {
                "detail": "Membership activated.",
                "membership": CompanyMembershipSerializer(membership, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )
