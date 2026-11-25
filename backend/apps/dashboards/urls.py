from django.urls import path
from .views import CeoDashboardView, DashboardSummaryView, RoleDashboardView

urlpatterns = [
    path("summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("ceo/", CeoDashboardView.as_view(), name="dashboard-ceo"),
    path("role/", RoleDashboardView.as_view(), name="dashboard-role"),
]
