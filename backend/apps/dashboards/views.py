from datetime import timedelta
from decimal import Decimal
from django.db.models import Avg, Count, Exists, OuterRef, Q, Sum
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.common.permissions import (
    IsCEOOnly,
    IsCompanyMemberOrAllowAnyInDev,
    get_ordered_user_roles,
    get_primary_role,
    get_user_capabilities,
)
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import Notification
from apps.organization.models import Department, DepartmentMembership, Team, TeamMembership
from apps.projects.models import Project, ProjectMember
from apps.projects.selectors import get_project_list_queryset
from apps.risks.models import Blocker, Issue, Risk
from apps.tasks.models import Task, TaskDependency
from apps.tasks.selectors import get_task_list_queryset
from apps.tasks.services.dependencies import STARTED_STATUSES, TERMINAL_STATUSES
from apps.tracking.models import ProgressUpdate, TimeEntry


def _annotate_dependency_blocked(tasks_qs):
    started_list = list(STARTED_STATUSES)
    terminal_list = list(TERMINAL_STATUSES)
    unsat_start_subq = TaskDependency.objects.filter(task_id=OuterRef("pk")).filter(
        (
            Q(dependency_type=TaskDependency.DependencyType.FINISH_TO_START)
            & ~Q(depends_on_task__status=Task.Status.COMPLETED)
        )
        | (
            Q(dependency_type=TaskDependency.DependencyType.START_TO_START)
            & ~Q(depends_on_task__status__in=started_list)
        )
    )
    unsat_complete_subq = TaskDependency.objects.filter(task_id=OuterRef("pk")).filter(
        (
            Q(
                dependency_type__in=[
                    TaskDependency.DependencyType.FINISH_TO_START,
                    TaskDependency.DependencyType.FINISH_TO_FINISH,
                ]
            )
            & ~Q(depends_on_task__status=Task.Status.COMPLETED)
        )
        | (
            Q(
                dependency_type__in=[
                    TaskDependency.DependencyType.START_TO_START,
                    TaskDependency.DependencyType.START_TO_FINISH,
                ]
            )
            & ~Q(depends_on_task__status__in=started_list)
        )
    )
    annotated = tasks_qs.annotate(
        _unsat_start=Exists(unsat_start_subq),
        _unsat_complete=Exists(unsat_complete_subq),
    )
    dep_blocked_q = (
        (~Q(status__in=started_list) & ~Q(status__in=terminal_list) & Q(_unsat_start=True))
        | (Q(status__in=started_list) & ~Q(status__in=terminal_list) & Q(_unsat_complete=True))
    )
    return annotated, dep_blocked_q, terminal_list


def _to_float(val, default=0.0):
    if val is None:
        return default
    if isinstance(val, Decimal):
        return float(val)
    return float(val)


class DashboardSummaryView(APIView):
    """
    Implements DB Design §6 & §8:
    'Dashboard: aggregate counts and progress at company/project/department/team/employee
    levels using filtered querysets and database aggregation. Ensure dashboard filters use
    the same authorization rules as task lists.'
    """

    permission_classes = [IsCompanyMemberOrAllowAnyInDev]

    def get(self, request):
        projects_qs = get_project_list_queryset(request)
        tasks_qs = get_task_list_queryset(request)

        project_id = request.query_params.get("project")
        department_id = request.query_params.get("department")
        team_id = request.query_params.get("team")
        employee_id = request.query_params.get("employee")

        if project_id:
            projects_qs = projects_qs.filter(id=project_id)
            tasks_qs = tasks_qs.filter(project_id=project_id)
        if department_id:
            tasks_qs = tasks_qs.filter(department_id=department_id)
        if team_id:
            tasks_qs = tasks_qs.filter(team_id=team_id)
        if employee_id:
            tasks_qs = tasks_qs.filter(
                Q(task_owner_id=employee_id)
                | Q(assignments__user_id=employee_id, assignments__status__in=["Active", "Acknowledged"])
            ).distinct()

        today = timezone.now().date()

        project_stats = projects_qs.aggregate(
            total_projects=Count("id", distinct=True),
            active_projects=Count("id", filter=Q(status="Active"), distinct=True),
            completed_projects=Count("id", filter=Q(status="Completed"), distinct=True),
            at_risk_projects=Count("id", filter=Q(status__in=["At Risk", "Delayed"]), distinct=True),
            average_project_progress=Avg("progress"),
            total_budget=Sum("budget"),
        )

        annotated_tasks_qs, dep_blocked_q, terminal_list = _annotate_dependency_blocked(tasks_qs)

        task_stats = annotated_tasks_qs.aggregate(
            total_tasks_and_subtasks=Count("id", distinct=True),
            top_level_tasks=Count("id", filter=Q(parent_task__isnull=True), distinct=True),
            subtasks=Count("id", filter=Q(parent_task__isnull=False), distinct=True),
            completed_tasks=Count("id", filter=Q(status="Completed"), distinct=True),
            in_progress_tasks=Count("id", filter=Q(status="In Progress"), distinct=True),
            blocked_tasks=Count("id", filter=Q(status="Blocked"), distinct=True),
            dependency_blocked_tasks=Count("id", filter=dep_blocked_q, distinct=True),
            overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            critical_overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today)
                & ~Q(status__in=terminal_list)
                & (Q(priority="Critical") | Q(due_date__lt=today - timedelta(days=7))),
                distinct=True,
            ),
            due_today_tasks=Count(
                "id",
                filter=Q(due_date=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            due_soon_tasks=Count(
                "id",
                filter=Q(due_date__gt=today, due_date__lte=today + timedelta(days=3))
                & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            average_task_progress=Avg("progress"),
            total_estimated_effort=Sum("estimated_effort"),
        )

        by_status = list(
            tasks_qs.values("status").annotate(count=Count("id", distinct=True)).order_by("status")
        )
        by_priority = list(
            tasks_qs.values("priority").annotate(count=Count("id", distinct=True)).order_by("priority")
        )

        return Response(
            {
                "projects": project_stats,
                "tasks": task_stats,
                "tasks_by_status": by_status,
                "tasks_by_priority": by_priority,
            }
        )


class CeoDashboardView(APIView):
    """
    SRS Module 8 (§10 — CEO Executive Dashboard):
    Strictly restricted to CEO / Superuser (`IsCEOOnly`).
    Provides cross-company consolidated KPIs, company drill-down, department performance,
    project portfolio health, approval bottlenecks, and executive alerts.
    """

    permission_classes = [IsCEOOnly]

    def get(self, request):
        today = timezone.now().date()
        company_id = request.query_params.get("company")
        terminal_list = list(TERMINAL_STATUSES)

        companies_qs = Company.objects.all().order_by("name")
        scoped_companies = companies_qs.filter(id=company_id) if company_id else companies_qs

        departments_qs = Department.objects.select_related("company").all()
        teams_qs = Team.objects.select_related("department", "department__company").all()
        projects_qs = Project.objects.select_related("company", "project_manager").all()
        tasks_qs = Task.objects.select_related(
            "project", "project__company", "department", "team", "task_owner", "reviewer"
        ).all()
        blockers_qs = Blocker.objects.select_related("task", "task__project").filter(
            resolution_status__in=["Open", "In Progress"]
        )
        risks_qs = Risk.objects.select_related("project").exclude(status="Closed")
        issues_qs = Issue.objects.select_related("project").exclude(status="Closed")

        if company_id:
            departments_qs = departments_qs.filter(company_id=company_id)
            teams_qs = teams_qs.filter(department__company_id=company_id)
            projects_qs = projects_qs.filter(company_id=company_id)
            tasks_qs = tasks_qs.filter(project__company_id=company_id)
            blockers_qs = blockers_qs.filter(task__project__company_id=company_id)
            risks_qs = risks_qs.filter(project__company_id=company_id)
            issues_qs = issues_qs.filter(project__company_id=company_id)

        annotated_tasks, dep_blocked_q, _ = _annotate_dependency_blocked(tasks_qs)
        proj_agg = projects_qs.aggregate(
            total_projects=Count("id", distinct=True),
            active_projects=Count("id", filter=Q(status="Active"), distinct=True),
            completed_projects=Count("id", filter=Q(status="Completed"), distinct=True),
            at_risk_projects=Count("id", filter=Q(status__in=["At Risk", "Delayed"]), distinct=True),
            avg_progress=Avg("progress"),
            total_budget=Sum("budget"),
        )
        task_agg = annotated_tasks.aggregate(
            total_tasks=Count("id", distinct=True),
            completed_tasks=Count("id", filter=Q(status="Completed"), distinct=True),
            in_progress_tasks=Count("id", filter=Q(status="In Progress"), distinct=True),
            blocked_tasks=Count("id", filter=Q(status="Blocked"), distinct=True),
            dependency_blocked_tasks=Count("id", filter=dep_blocked_q, distinct=True),
            pending_reviews=Count("id", filter=Q(status="In Review"), distinct=True),
            changes_requested=Count("id", filter=Q(status="Changes Requested"), distinct=True),
            overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            critical_overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today)
                & ~Q(status__in=terminal_list)
                & (Q(priority="Critical") | Q(due_date__lt=today - timedelta(days=7))),
                distinct=True,
            ),
            due_today_tasks=Count(
                "id",
                filter=Q(due_date=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            avg_task_progress=Avg("progress"),
        )

        users_qs = User.objects.all()
        if company_id:
            users_qs = users_qs.filter(
                company_memberships__company_id=company_id,
                company_memberships__status=CompanyMembership.Status.ACTIVE,
            ).distinct()

        enterprise_kpis = {
            "total_companies": companies_qs.count(),
            "active_companies": companies_qs.filter(status=Company.Status.ACTIVE).count(),
            "total_users": users_qs.count(),
            "active_users": users_qs.filter(is_active=True, account_status=User.AccountStatus.ACTIVE).count(),
            "total_departments": departments_qs.count(),
            "total_teams": teams_qs.count(),
            "total_projects": proj_agg["total_projects"] or 0,
            "active_projects": proj_agg["active_projects"] or 0,
            "completed_projects": proj_agg["completed_projects"] or 0,
            "at_risk_projects": proj_agg["at_risk_projects"] or 0,
            "avg_project_progress": round(_to_float(proj_agg["avg_progress"]), 1),
            "total_budget": round(_to_float(proj_agg["total_budget"]), 2),
            "total_tasks": task_agg["total_tasks"] or 0,
            "completed_tasks": task_agg["completed_tasks"] or 0,
            "in_progress_tasks": task_agg["in_progress_tasks"] or 0,
            "blocked_tasks": task_agg["blocked_tasks"] or 0,
            "dependency_blocked_tasks": task_agg["dependency_blocked_tasks"] or 0,
            "pending_reviews": task_agg["pending_reviews"] or 0,
            "changes_requested": task_agg["changes_requested"] or 0,
            "overdue_tasks": task_agg["overdue_tasks"] or 0,
            "critical_overdue_tasks": task_agg["critical_overdue_tasks"] or 0,
            "due_today_tasks": task_agg["due_today_tasks"] or 0,
            "avg_task_progress": round(_to_float(task_agg["avg_task_progress"]), 1),
            "open_blockers": blockers_qs.count(),
            "open_risks": risks_qs.count(),
            "open_issues": issues_qs.count(),
        }

        # Company-by-company executive breakdown
        company_breakdown = []
        for comp in scoped_companies:
            c_projects = Project.objects.filter(company=comp)
            c_tasks = Task.objects.filter(project__company=comp)
            c_total_t = c_tasks.count()
            c_done_t = c_tasks.filter(status=Task.Status.COMPLETED).count()
            c_overdue_t = c_tasks.filter(due_date__lt=today).exclude(status__in=terminal_list).count()
            c_proj_agg = c_projects.aggregate(
                avg_prog=Avg("progress"),
                budget_sum=Sum("budget"),
            )
            company_breakdown.append(
                {
                    "id": str(comp.id),
                    "name": comp.name,
                    "code": comp.code,
                    "status": comp.status,
                    "is_active": comp.status == Company.Status.ACTIVE,
                    "members_count": CompanyMembership.objects.filter(
                        company=comp, status=CompanyMembership.Status.ACTIVE
                    ).count(),
                    "departments_count": Department.objects.filter(company=comp, status="Active").count(),
                    "teams_count": Team.objects.filter(department__company=comp, status="Active").count(),
                    "projects_count": c_projects.count(),
                    "active_projects_count": c_projects.filter(status="Active").count(),
                    "at_risk_projects_count": c_projects.filter(status__in=["At Risk", "Delayed"]).count(),
                    "tasks_count": c_total_t,
                    "completed_tasks_count": c_done_t,
                    "overdue_tasks_count": c_overdue_t,
                    "completion_rate": round((c_done_t / c_total_t * 100.0) if c_total_t else 0.0, 1),
                    "avg_progress": round(_to_float(c_proj_agg["avg_prog"]), 1),
                    "total_budget": round(_to_float(c_proj_agg["budget_sum"]), 2),
                }
            )

        # Department breakdown
        department_breakdown = []
        for dept in departments_qs[:20]:
            head_dm = DepartmentMembership.objects.filter(
                department=dept, is_head=True, status=DepartmentMembership.Status.ACTIVE
            ).select_related("user").first()
            d_tasks = tasks_qs.filter(department=dept)
            d_total = d_tasks.count()
            d_done = d_tasks.filter(status=Task.Status.COMPLETED).count()
            d_overdue = d_tasks.filter(due_date__lt=today).exclude(status__in=terminal_list).count()
            department_breakdown.append(
                {
                    "id": str(dept.id),
                    "name": dept.name,
                    "code": dept.code,
                    "company_id": str(dept.company_id),
                    "company_name": dept.company.name,
                    "status": dept.status,
                    "head_name": head_dm.user.full_name if head_dm else "Unassigned",
                    "teams_count": Team.objects.filter(department=dept, status="Active").count(),
                    "members_count": DepartmentMembership.objects.filter(
                        department=dept, status=DepartmentMembership.Status.ACTIVE
                    ).count(),
                    "tasks_count": d_total,
                    "completed_tasks_count": d_done,
                    "overdue_tasks_count": d_overdue,
                    "completion_rate": round((d_done / d_total * 100.0) if d_total else 0.0, 1),
                }
            )

        # Project portfolio health
        project_health = []
        for proj in projects_qs.order_by("-priority", "planned_completion_date")[:15]:
            p_tasks = Task.objects.filter(project=proj)
            p_total = p_tasks.count()
            p_done = p_tasks.filter(status=Task.Status.COMPLETED).count()
            p_overdue = p_tasks.filter(due_date__lt=today).exclude(status__in=terminal_list).count()
            project_health.append(
                {
                    "id": str(proj.id),
                    "code": proj.code,
                    "name": proj.name,
                    "company_name": proj.company.name,
                    "project_manager_name": proj.project_manager.full_name if proj.project_manager else "Unassigned",
                    "status": proj.status,
                    "priority": proj.priority,
                    "progress": round(_to_float(proj.progress), 1),
                    "budget": round(_to_float(proj.budget), 2) if proj.budget is not None else None,
                    "planned_completion_date": proj.planned_completion_date.isoformat() if proj.planned_completion_date else None,
                    "tasks_count": p_total,
                    "completed_tasks_count": p_done,
                    "overdue_tasks_count": p_overdue,
                }
            )

        # Pending approvals & critical overdue tasks
        pending_approvals = [
            {
                "id": str(t.id),
                "title": t.title,
                "project_id": str(t.project_id),
                "project_code": t.project.code,
                "company_name": t.project.company.name,
                "priority": t.priority,
                "owner_name": t.task_owner.full_name if t.task_owner else "Unassigned",
                "reviewer_name": t.reviewer.full_name if t.reviewer else "Unassigned",
                "due_date": t.due_date.isoformat() if t.due_date else None,
            }
            for t in tasks_qs.filter(status=Task.Status.IN_REVIEW).order_by("due_date")[:10]
        ]

        overdue_highlights = [
            {
                "id": str(t.id),
                "title": t.title,
                "project_id": str(t.project_id),
                "project_code": t.project.code,
                "company_name": t.project.company.name,
                "status": t.status,
                "priority": t.priority,
                "owner_name": t.task_owner.full_name if t.task_owner else "Unassigned",
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "days_overdue": (today - t.due_date).days if t.due_date else 0,
            }
            for t in tasks_qs.filter(due_date__lt=today)
            .exclude(status__in=terminal_list)
            .order_by("due_date")[:10]
        ]

        by_status = list(
            tasks_qs.values("status").annotate(count=Count("id", distinct=True)).order_by("status")
        )
        by_priority = list(
            tasks_qs.values("priority").annotate(count=Count("id", distinct=True)).order_by("priority")
        )

        return Response(
            {
                "selected_company": company_id,
                "companies_filter": [
                    {"id": str(c.id), "name": c.name, "code": c.code, "status": c.status}
                    for c in companies_qs
                ],
                "enterprise_kpis": enterprise_kpis,
                "company_breakdown": company_breakdown,
                "department_breakdown": department_breakdown,
                "project_health": project_health,
                "pending_approvals": pending_approvals,
                "overdue_highlights": overdue_highlights,
                "tasks_by_status": by_status,
                "tasks_by_priority": by_priority,
            }
        )


class RoleDashboardView(APIView):
    """
    SRS Module 9 (§11 — Role-Based Dashboards):
    Returns a tailored operational dashboard strictly scoped to the authenticated user's role:
    - ProjectManager: Managed projects, milestones, delivery progress, review queue, team load
    - DepartmentHead: Department teams, members, cross-project department tasks, review queue
    - TeamLeader: Team tasks, member workload, overdue/blocked alerts, team reviews
    - Employee: Personal assigned/owned tasks, due today/soon, changes requested, personal time logs
    - CEO / CompanyInCharge: Executive / company summary with quick links
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        today = timezone.now().date()
        seven_days_ago = timezone.now() - timedelta(days=7)
        primary_role = get_primary_role(user)
        roles = get_ordered_user_roles(user)
        capabilities = get_user_capabilities(user)
        terminal_list = list(TERMINAL_STATUSES)

        company_ids = list(
            CompanyMembership.objects.filter(
                user=user, status=CompanyMembership.Status.ACTIVE
            ).values_list("company_id", flat=True)
        )
        dept_memberships = list(
            DepartmentMembership.objects.filter(
                user=user, status=DepartmentMembership.Status.ACTIVE
            ).select_related("department", "department__company")
        )
        team_memberships = list(
            TeamMembership.objects.filter(
                user=user, status=TeamMembership.Status.ACTIVE
            ).select_related("team", "team__department")
        )
        my_dept_ids = [dm.department_id for dm in dept_memberships]
        headed_dept_ids = [dm.department_id for dm in dept_memberships if dm.is_head]
        my_team_ids = [tm.team_id for tm in team_memberships]
        led_team_ids = [tm.team_id for tm in team_memberships if tm.is_leader]

        base_projects = get_project_list_queryset(request)
        base_tasks = get_task_list_queryset(request)

        # Scope projects and tasks according to role
        if primary_role == "CEO":
            scope_title = "Enterprise Executive Scope (All Companies)"
            scoped_projects = base_projects
            scoped_tasks = base_tasks
        elif primary_role == "CompanyInCharge":
            scope_title = "Company Operations Scope"
            scoped_projects = base_projects.filter(company_id__in=company_ids)
            scoped_tasks = base_tasks.filter(project__company_id__in=company_ids)
        elif primary_role == "ProjectManager":
            scope_title = "Project Manager Delivery Scope"
            managed_q = Q(project_manager=user) | Q(
                members__user=user, members__status=ProjectMember.Status.ACTIVE
            )
            scoped_projects = base_projects.filter(managed_q).distinct()
            scoped_tasks = base_tasks.filter(project__in=scoped_projects).distinct()
        elif primary_role == "DepartmentHead":
            target_dept_ids = headed_dept_ids or my_dept_ids
            dept_names = [
                dm.department.name
                for dm in dept_memberships
                if dm.department_id in target_dept_ids
            ]
            scope_title = (
                f"Department Head Scope · {', '.join(dept_names)}"
                if dept_names
                else "Department Head Scope"
            )
            scoped_projects = base_projects.filter(
                Q(project_departments__department_id__in=target_dept_ids)
                | Q(members__user=user, members__status=ProjectMember.Status.ACTIVE)
            ).distinct()
            scoped_tasks = base_tasks.filter(
                Q(department_id__in=target_dept_ids)
                | Q(team__department_id__in=target_dept_ids)
                | Q(reviewer=user)
                | Q(task_owner=user)
            ).distinct()
        elif primary_role == "TeamLeader":
            target_team_ids = led_team_ids or my_team_ids
            team_names = [
                tm.team.name
                for tm in team_memberships
                if tm.team_id in target_team_ids
            ]
            scope_title = (
                f"Team Leader Scope · {', '.join(team_names)}"
                if team_names
                else "Team Leader Scope"
            )
            scoped_projects = base_projects.filter(
                Q(project_teams__team_id__in=target_team_ids)
                | Q(members__user=user, members__status=ProjectMember.Status.ACTIVE)
            ).distinct()
            scoped_tasks = base_tasks.filter(
                Q(team_id__in=target_team_ids)
                | Q(assignments__team_id__in=target_team_ids, assignments__status__in=["Active", "Acknowledged"])
                | Q(reviewer=user)
                | Q(task_owner=user)
                | Q(assignments__user=user, assignments__status__in=["Active", "Acknowledged"])
            ).distinct()
        else:
            # Employee: strictly personal scope (own tasks & participating projects)
            scope_title = "My Personal Task Workspace"
            scoped_projects = base_projects.filter(
                members__user=user, members__status=ProjectMember.Status.ACTIVE
            ).distinct()
            scoped_tasks = base_tasks.filter(
                Q(task_owner=user)
                | Q(assignments__user=user, assignments__status__in=["Active", "Acknowledged"])
                | Q(assignments__team_id__in=my_team_ids, assignments__status__in=["Active", "Acknowledged"])
            ).distinct()

        # Personal tasks (always strictly user's own assigned/owned tasks)
        personal_tasks_qs = base_tasks.filter(
            Q(task_owner=user)
            | Q(assignments__user=user, assignments__status__in=["Active", "Acknowledged"])
        ).distinct()

        # Review queue (only for reviewer/lead roles)
        review_queue = []
        if capabilities["can_review_tasks"]:
            rev_qs = scoped_tasks.filter(status=Task.Status.IN_REVIEW)
            if primary_role in ("TeamLeader", "DepartmentHead"):
                rev_qs = rev_qs.filter(Q(reviewer=user) | Q(reviewer__isnull=True) | Q(id__in=scoped_tasks.values("id")))
            for t in rev_qs.order_by("due_date")[:10]:
                review_queue.append(
                    {
                        "id": str(t.id),
                        "title": t.title,
                        "project_id": str(t.project_id),
                        "project_code": t.project.code,
                        "priority": t.priority,
                        "owner_name": t.task_owner.full_name if t.task_owner else "Unassigned",
                        "reviewer_name": t.reviewer.full_name if t.reviewer else "Unassigned",
                        "due_date": t.due_date.isoformat() if t.due_date else None,
                    }
                )

        # Team / Member workload (only for leadership roles, never Employees)
        team_workload = []
        if capabilities["can_view_workload"]:
            if primary_role == "TeamLeader" and (led_team_ids or my_team_ids):
                member_users = User.objects.filter(
                    team_memberships__team_id__in=(led_team_ids or my_team_ids),
                    team_memberships__status=TeamMembership.Status.ACTIVE,
                ).distinct()
            elif primary_role == "DepartmentHead" and (headed_dept_ids or my_dept_ids):
                member_users = User.objects.filter(
                    department_memberships__department_id__in=(headed_dept_ids or my_dept_ids),
                    department_memberships__status=DepartmentMembership.Status.ACTIVE,
                ).distinct()
            else:
                member_users = User.objects.filter(
                    company_memberships__company_id__in=company_ids,
                    company_memberships__status=CompanyMembership.Status.ACTIVE,
                ).distinct()

            for member in member_users[:15]:
                m_open = scoped_tasks.filter(
                    Q(task_owner=member)
                    | Q(assignments__user=member, assignments__status__in=["Active", "Acknowledged"])
                ).exclude(status__in=terminal_list).distinct()
                m_overdue = m_open.filter(due_date__lt=today).count()
                m_effort = m_open.aggregate(total=Sum("estimated_effort"))["total"]
                team_workload.append(
                    {
                        "id": str(member.id),
                        "full_name": member.full_name,
                        "email": member.email,
                        "designation": member.designation,
                        "open_tasks": m_open.count(),
                        "overdue_tasks": m_overdue,
                        "estimated_hours": round(_to_float(m_effort), 1),
                    }
                )

        # Aggregate KPIs
        annotated_scoped, dep_blocked_q, _ = _annotate_dependency_blocked(scoped_tasks)
        t_agg = annotated_scoped.aggregate(
            total_tasks=Count("id", distinct=True),
            completed_tasks=Count("id", filter=Q(status="Completed"), distinct=True),
            in_progress_tasks=Count("id", filter=Q(status="In Progress"), distinct=True),
            blocked_tasks=Count("id", filter=Q(status="Blocked"), distinct=True),
            dependency_blocked_tasks=Count("id", filter=dep_blocked_q, distinct=True),
            pending_reviews=Count("id", filter=Q(status="In Review"), distinct=True),
            changes_requested=Count("id", filter=Q(status="Changes Requested"), distinct=True),
            overdue_tasks=Count(
                "id",
                filter=Q(due_date__lt=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            due_today_tasks=Count(
                "id",
                filter=Q(due_date=today) & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            due_soon_tasks=Count(
                "id",
                filter=Q(due_date__gt=today, due_date__lte=today + timedelta(days=3))
                & ~Q(status__in=terminal_list),
                distinct=True,
            ),
            avg_progress=Avg("progress"),
        )
        p_agg = scoped_projects.aggregate(
            total_projects=Count("id", distinct=True),
            active_projects=Count("id", filter=Q(status="Active"), distinct=True),
            at_risk_projects=Count("id", filter=Q(status__in=["At Risk", "Delayed"]), distinct=True),
            avg_project_progress=Avg("progress"),
        )
        logged_7d = TimeEntry.objects.filter(
            user=user, created_at__gte=seven_days_ago
        ).aggregate(total=Sum("duration_minutes"))["total"] or 0

        my_open_tasks = [
            {
                "id": str(t.id),
                "title": t.title,
                "project_id": str(t.project_id),
                "project_code": t.project.code,
                "project_name": t.project.name,
                "status": t.status,
                "priority": t.priority,
                "progress": round(_to_float(t.progress), 0),
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "is_overdue": bool(t.due_date and t.due_date < today and t.status not in terminal_list),
                "approval_required": t.approval_required,
            }
            for t in personal_tasks_qs.exclude(status__in=terminal_list).order_by("due_date", "-priority")[:12]
        ]

        projects_list = [
            {
                "id": str(p.id),
                "code": p.code,
                "name": p.name,
                "company_name": p.company.name,
                "project_manager_name": p.project_manager.full_name if p.project_manager else "Unassigned",
                "status": p.status,
                "priority": p.priority,
                "progress": round(_to_float(p.progress), 1),
                "planned_completion_date": p.planned_completion_date.isoformat() if p.planned_completion_date else None,
            }
            for p in scoped_projects.order_by("-priority", "planned_completion_date")[:10]
        ]

        upcoming_deadlines = [
            {
                "id": str(t.id),
                "title": t.title,
                "project_id": str(t.project_id),
                "project_code": t.project.code,
                "status": t.status,
                "priority": t.priority,
                "owner_name": t.task_owner.full_name if t.task_owner else "Unassigned",
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "days_until_due": (t.due_date - today).days if t.due_date else None,
            }
            for t in scoped_tasks.filter(
                due_date__isnull=False, due_date__lte=today + timedelta(days=7)
            )
            .exclude(status__in=terminal_list)
            .order_by("due_date")[:10]
        ]

        unread_notifications = Notification.objects.filter(
            user=user, is_read=False
        ).count()

        recent_updates = [
            {
                "id": str(pu.id),
                "task_id": str(pu.task_id),
                "task_title": pu.task.title,
                "author_name": pu.author.full_name if pu.author else "System",
                "progress_percent": round(_to_float(pu.progress_percentage), 0),
                "work_completed": pu.work_completed or pu.remarks or "",
                "created_at": pu.created_at.isoformat(),
            }
            for pu in ProgressUpdate.objects.filter(task__in=scoped_tasks)
            .select_related("task", "author")
            .order_by("-created_at")[:6]
        ]

        by_status = list(
            scoped_tasks.values("status").annotate(count=Count("id", distinct=True)).order_by("status")
        )
        by_priority = list(
            scoped_tasks.values("priority").annotate(count=Count("id", distinct=True)).order_by("priority")
        )

        return Response(
            {
                "role": primary_role,
                "roles": roles,
                "capabilities": capabilities,
                "scope_title": scope_title,
                "user": {
                    "id": str(user.id),
                    "full_name": user.full_name,
                    "email": user.email,
                    "username": user.username,
                    "employee_id": user.employee_id,
                    "designation": user.designation,
                },
                "kpis": {
                    "total_projects": p_agg["total_projects"] or 0,
                    "active_projects": p_agg["active_projects"] or 0,
                    "at_risk_projects": p_agg["at_risk_projects"] or 0,
                    "avg_project_progress": round(_to_float(p_agg["avg_project_progress"]), 1),
                    "total_tasks": t_agg["total_tasks"] or 0,
                    "completed_tasks": t_agg["completed_tasks"] or 0,
                    "in_progress_tasks": t_agg["in_progress_tasks"] or 0,
                    "blocked_tasks": t_agg["blocked_tasks"] or 0,
                    "dependency_blocked_tasks": t_agg["dependency_blocked_tasks"] or 0,
                    "pending_reviews": t_agg["pending_reviews"] or 0,
                    "changes_requested": t_agg["changes_requested"] or 0,
                    "overdue_tasks": t_agg["overdue_tasks"] or 0,
                    "due_today_tasks": t_agg["due_today_tasks"] or 0,
                    "due_soon_tasks": t_agg["due_soon_tasks"] or 0,
                    "avg_task_progress": round(_to_float(t_agg["avg_progress"]), 1),
                    "logged_hours_7d": round(logged_7d / 60.0, 1),
                    "unread_notifications": unread_notifications,
                },
                "projects": projects_list,
                "my_tasks": my_open_tasks,
                "review_queue": review_queue,
                "team_workload": team_workload,
                "upcoming_deadlines": upcoming_deadlines,
                "recent_updates": recent_updates,
                "tasks_by_status": by_status,
                "tasks_by_priority": by_priority,
            }
        )
