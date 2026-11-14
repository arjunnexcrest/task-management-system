import csv
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.tasks.selectors import get_task_list_queryset


class TaskExportReportView(APIView):
    """
    Company-scoped task report and CSV export endpoint (DB Design §7 Rule #5 & §8).
    """

    permission_classes = [IsCompanyMemberOrAllowAnyInDev]

    def get(self, request):
        tasks_qs = get_task_list_queryset(request).order_by("-created_at")
        export_format = request.query_params.get("format", "json").lower()

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
