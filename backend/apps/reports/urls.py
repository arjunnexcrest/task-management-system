from django.urls import path
from .views import TaskExportReportView

urlpatterns = [
    path("tasks/", TaskExportReportView.as_view(), name="report-tasks"),
]
