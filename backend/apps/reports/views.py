import csv
import json
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.common.permissions import (
    MANAGEMENT_ROLES,
    IsActiveUser,
    IsCompanyMember,
    get_company_id_from_request,
    user_has_any_role,
)
from apps.tasks.filters import TaskFilterSet
from apps.tasks.selectors import get_task_list_queryset
from apps.tasks.services import (
    compute_task_schedule_metrics,
    evaluate_task_dependencies,
    get_company_working_calendar,
)


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
    Company- & project-scoped task report and CSV export endpoint
    (SRS §2.8, §16.5, §27.3 & DB Design §7 Rule #5, §8).
    Restricted to leadership & reporting roles (CEO, CompanyInCharge, ProjectManager,
    DepartmentHead, TeamLeader); regular Employees receive 403 Forbidden.
    """

    permission_classes = [IsAuthenticated, IsActiveUser, IsCompanyMember]
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer, CSVPassThroughRenderer]

    def get(self, request):
        company_id = get_company_id_from_request(request)
        if not user_has_any_role(request.user, MANAGEMENT_ROLES, company_id=company_id):
            return JsonResponse(
                {
                    "detail": "Reports and exports are available only to Team Leaders, Department Heads, Project Managers, Company In-Charge, and CEO."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        base_qs = get_task_list_queryset(request).order_by("-created_at")
        tasks_qs = TaskFilterSet(request.query_params, queryset=base_qs, request=request).qs
        search_q = (request.query_params.get("search") or "").strip()
        if search_q:
            tasks_qs = tasks_qs.filter(
                Q(title__icontains=search_q)
                | Q(project__code__icontains=search_q)
                | Q(project__name__icontains=search_q)
            )
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
