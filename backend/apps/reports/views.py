import csv
from django.http import HttpResponse
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer, JSONRenderer
from rest_framework.views import APIView
from rest_framework.response import Response
from apps.common.permissions import IsCompanyMemberOrAllowAnyInDev
from apps.tasks.selectors import get_task_list_queryset
from apps.tasks.services import (
    compute_task_schedule_metrics,
    evaluate_task_dependencies,
    get_company_working_calendar,
)


class PassThroughCSVRenderer(BaseRenderer):
    media_type = "text/csv"
    format = "csv"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


class TaskExportReportView(APIView):
    """
    Company-scoped task report and CSV export endpoint (DB Design §7 Rule #5 & §8, SRS §27.3).
    Includes overdue calculation (calendar & working days) and dependency-blocked status.
    """

    permission_classes = [IsCompanyMemberOrAllowAnyInDev]
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer, PassThroughCSVRenderer]

    def get(self, request):
        tasks_qs = get_task_list_queryset(request).order_by("-created_at")
        export_format = (
            request.query_params.get("export")
            or request.query_params.get("format")
            or "json"
        ).lower()
        cal_cache: dict = {}

        def _metrics_and_deps(task):
            comp = task.project.company
            if comp.id not in cal_cache:
                cal_cache[comp.id] = get_company_working_calendar(comp)
            metrics = compute_task_schedule_metrics(task, calendar_cfg=cal_cache[comp.id])
            deps = evaluate_task_dependencies(task)
            return metrics, deps

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
                    "Start Date",
                    "Due Date",
                    "Due Status",
                    "Is Overdue",
                    "Days Overdue",
                    "Working Days Overdue",
                    "Overdue Severity",
                    "Dependency Blocked",
                    "Blocking Prerequisites",
                ]
            )
            for t in tasks_qs[:1000]:
                m, d = _metrics_and_deps(t)
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
                        str(t.start_date or ""),
                        str(t.due_date or ""),
                        m["due_status"],
                        m["is_overdue"],
                        m["days_overdue"],
                        m["working_days_overdue"],
                        m["overdue_severity"],
                        d["is_dependency_blocked"],
                        d["blocking_prerequisites_count"],
                    ]
                )
            return response

        data = []
        for t in tasks_qs[:200]:
            m, d = _metrics_and_deps(t)
            data.append(
                {
                    "id": t.id,
                    "project_code": t.project.code,
                    "title": t.title,
                    "is_subtask": t.is_subtask,
                    "status": t.status,
                    "priority": t.priority,
                    "progress": t.progress,
                    "owner": t.task_owner.full_name if t.task_owner else None,
                    "start_date": t.start_date,
                    "due_date": t.due_date,
                    "due_status": m["due_status"],
                    "is_overdue": m["is_overdue"],
                    "days_overdue": m["days_overdue"],
                    "working_days_overdue": m["working_days_overdue"],
                    "overdue_severity": m["overdue_severity"],
                    "is_dependency_blocked": d["is_dependency_blocked"],
                    "blocking_prerequisites_count": d["blocking_prerequisites_count"],
                }
            )
        return Response({"count": len(data), "results": data})
