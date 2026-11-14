from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import BlockerViewSet, IssueViewSet, RiskViewSet

router = DefaultRouter()
router.register(r"blockers", BlockerViewSet, basename="blocker")
router.register(r"risks", RiskViewSet, basename="risk")
router.register(r"issues", IssueViewSet, basename="issue")

urlpatterns = [
    path("", include(router.urls)),
]
