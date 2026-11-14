from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import CompanyMembershipViewSet, CompanyViewSet

router = DefaultRouter()
router.register(r"memberships", CompanyMembershipViewSet, basename="company-membership")
router.register(r"", CompanyViewSet, basename="company")

urlpatterns = [
    path("", include(router.urls)),
]
