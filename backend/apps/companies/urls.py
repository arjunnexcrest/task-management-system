from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import CompanyMembershipViewSet, CompanyViewSet

router = DefaultRouter()
router.register(r"memberships", CompanyMembershipViewSet, basename="company-membership")
router.register(r"", CompanyViewSet, basename="company")

membership_router = DefaultRouter()
membership_router.register(r"", CompanyMembershipViewSet, basename="membership")

urlpatterns = [
    path("", include(router.urls)),
]
