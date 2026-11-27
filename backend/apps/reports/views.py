import csv
import json
from django.http import HttpResponse, JsonResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer, JSONRenderer
from rest_framework.views import APIView
from rest_framework.response import Response
from apps.common.permissions import (
    MANAGEMENT_ROLES,
    IsActiveUser,
    IsCompanyMember,
    get_company_id_from_request,
    user_has_any_role,
)
from apps.tasks.selectors import get_task_list_queryset


class CSVPassThroughRenderer(BaseRenderer):
    """Allows DRF content negotiation to accept `?format=csv` on report export views."""

    media_type = "text/csv"
    format = "csv"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode(self.charset)
        return json.dumps(data).encode(self.charset)


class TaskExportReportView(APIView):
    """
    Company- & project-scoped task report and CSV export endpoint (SRS §2.8, §16.5 & DB Design §7 Rule #5).
    - JSON summary: available to authorized company members (scoped to authorized tasks).
    - CSV export (?format=csv or ?export=csv): restricted to management/reporting roles (CEO, CompanyInCharge,
      ProjectManager, DepartmentHead, TeamLeader). Regular Employees receive 403 Forbidden.
    """

    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer, CSVPassThroughRenderer]

    def get(self, request):
        export_format = (
            request.query_params.get("export") or request.query_params.get("format", "json")
        ).lower()

        if export_format == "csv":
            company_id = get_company_id_from_request(request)
            if not user_has_any_role(request.user, MANAGEMENT_ROLES, company_id=company_id):
                return JsonResponse(
                    {
                        "detail": "Only authorized reporting roles (CEO, Company In-Charge, Project Manager, Department Head, Team Leader) can export task reports to CSV."
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        tasks_qs = get_task_list_queryset(request).order_by("-created_at")

        if export_format == "csv":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = 'attachment; filename="tasks_report.csv"'
            writer = csv.writer(response)
            writer.writerow(
                [
                    "Task ID",
                    "Project Code",
                    "Title",
                    "Is Subtask",
                    "Status",
                    "Priority",
                    "Progress (%)",
                    "Owner",
                    "Due Date",
                ]
            )
            for t in tasks_qs[:1000]:
                writer.writerow(
                    [
                        str(t.id),
                        t.project.code,
                        t.title,
                        t.is_subtask,
                        t.status,
                        t.priority,
                        str(t.progress),
                        t.task_owner.full_name if t.task_owner else "",
                        str(t.due_date or ""),
                    ]
                )
            return response

        data = [
            {
                "id": t.id,
                "project_code": t.project.code,
                "title": t.title,
                "is_subtask": t.is_subtask,
                "status": t.status,
                "priority": t.priority,
                "progress": t.progress,
                "owner": t.task_owner.full_name if t.task_owner else None,
                "due_date": t.due_date,
            }
            for t in tasks_qs[:200]
        ]
        return Response({"count": len(data), "results": data})
