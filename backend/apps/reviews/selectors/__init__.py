from django.db.models import Count, Q
from apps.common.selectors import scope_queryset_by_company
from apps.companies.models import Company
from apps.projects.models import Project
from apps.reviews.models import TaskReview
from apps.reviews.services import compute_task_review_sla, get_approval_chain_rules
from apps.tasks.models import Task


def get_review_list_queryset(request=None):
    """
    DB Design §8:
    Paginated TaskReview history queryset with `select_related` & `prefetch_related`
    to eliminate N+1 queries and enforce active company isolation.
    """
    qs = (
        TaskReview.objects.select_related(
            "task",
            "task__project",
            "task__project__company",
            "task__task_owner",
            "reviewer",
        )
        .prefetch_related(
            "task__comments__comment_attachments__attachment__uploaded_by",
        )
        .order_by("-reviewed_at", "-review_round")
    )
    return scope_queryset_by_company(qs, request, company_lookup="task__project__company_id")


def get_review_queue_queryset(request=None, queue_tab: str = "pending"):
    """
    Screen #33 (Task Review and Approval) & SRS §11.2, §14.1-14.4:
    Retrieves scoped tasks for Review Queue tabs with full prefetched review history,
    comments, attachments, and assignments.
    """
    qs = (
        Task.objects.select_related(
            "project",
            "project__company",
            "project__project_manager",
            "parent_task",
            "department",
            "team",
            "task_owner",
            "created_by",
            "reviewer",
        )
        .prefetch_related(
            "reviews__reviewer",
            "assignments__user",
            "assignments__team",
            "task_attachments__attachment__uploaded_by",
            "comments__comment_attachments__attachment__uploaded_by",
            "subtasks",
        )
        .annotate(
            annotated_reviews_count=Count("reviews", distinct=True),
            annotated_attachments_count=Count("task_attachments", distinct=True),
        )
    )
    qs = scope_queryset_by_company(qs, request, company_lookup="project__company_id")

    tab = (queue_tab or "pending").lower().strip()
    if tab == "pending":
        qs = qs.filter(status=Task.Status.IN_REVIEW)
    elif tab == "changes_requested":
        qs = qs.filter(status=Task.Status.CHANGES_REQUESTED)
    elif tab == "approved":
        qs = qs.filter(status=Task.Status.APPROVED)
    elif tab == "ready_to_submit":
        qs = qs.filter(
            status__in=[Task.Status.IN_PROGRESS, Task.Status.ASSIGNED, Task.Status.TO_DO]
        ).filter(Q(approval_required=True) | Q(reviewer__isnull=False))
    elif tab == "all":
        qs = qs.filter(
            Q(status__in=[Task.Status.IN_REVIEW, Task.Status.CHANGES_REQUESTED, Task.Status.APPROVED])
            | Q(approval_required=True)
            | Q(reviews__isnull=False)
        ).distinct()

    if request is not None:
        qp = request.query_params
        if qp.get("project"):
            qs = qs.filter(project_id=qp["project"])
        if qp.get("reviewer"):
            qs = qs.filter(reviewer_id=qp["reviewer"])
        if qp.get("task_owner"):
            qs = qs.filter(task_owner_id=qp["task_owner"])
        if qp.get("priority"):
            qs = qs.filter(priority=qp["priority"])
        if qp.get("department"):
            qs = qs.filter(department_id=qp["department"])
        if qp.get("team"):
            qs = qs.filter(team_id=qp["team"])
        if qp.get("search"):
            s = qp["search"].strip()
            qs = qs.filter(
                Q(title__icontains=s)
                | Q(project__code__icontains=s)
                | Q(project__name__icontains=s)
                | Q(task_owner__full_name__icontains=s)
                | Q(reviewer__full_name__icontains=s)
            )

    return qs.order_by("due_date", "-updated_at")


def get_review_analytics_summary(request=None) -> dict:
    """
    SRS §14.4, §16.3 & §16.4:
    Returns aggregated review queue KPIs, SLA escalation counts, first-pass approval rates,
    reviewer workload, and employee correction trends.
    """
    tasks_qs = scope_queryset_by_company(
        Task.objects.select_related("project", "project__company", "task_owner", "reviewer"),
        request,
        company_lookup="project__company_id",
    )
    reviews_qs = scope_queryset_by_company(
        TaskReview.objects.select_related("task", "task__task_owner", "reviewer"),
        request,
        company_lookup="task__project__company_id",
    )

    company_obj = None
    project_obj = None
    if request is not None:
        cid = request.query_params.get("company")
        pid = request.query_params.get("project")
        if cid:
            tasks_qs = tasks_qs.filter(project__company_id=cid)
            reviews_qs = reviews_qs.filter(task__project__company_id=cid)
            company_obj = Company.objects.filter(id=cid).first()
        if pid:
            tasks_qs = tasks_qs.filter(project_id=pid)
            reviews_qs = reviews_qs.filter(task__project_id=pid)
            project_obj = Project.objects.select_related("company").filter(id=pid).first()
            if project_obj and not company_obj:
                company_obj = project_obj.company

    rules = get_approval_chain_rules(company=company_obj, project=project_obj)

    in_review_tasks = list(tasks_qs.filter(status=Task.Status.IN_REVIEW))
    changes_requested_count = tasks_qs.filter(status=Task.Status.CHANGES_REQUESTED).count()
    approved_ready_count = tasks_qs.filter(status=Task.Status.APPROVED).count()
    ready_to_submit_count = tasks_qs.filter(
        status__in=[Task.Status.IN_PROGRESS, Task.Status.ASSIGNED, Task.Status.TO_DO]
    ).filter(Q(approval_required=True) | Q(reviewer__isnull=False)).count()

    overdue_escalations_count = 0
    reviewer_pending_map: dict[str, dict] = {}
    for t in in_review_tasks:
        sla = compute_task_review_sla(t, threshold_hours=rules["escalation_hours"])
        if sla["is_review_overdue"]:
            overdue_escalations_count += 1
        if t.reviewer_id:
            rid = str(t.reviewer_id)
            entry = reviewer_pending_map.setdefault(
                rid,
                {
                    "reviewer_id": rid,
                    "reviewer_name": t.reviewer.full_name if t.reviewer else "Unassigned",
                    "pending_count": 0,
                    "approved_count": 0,
                    "returned_count": 0,
                },
            )
            entry["pending_count"] += 1

    all_reviews = list(reviews_qs)
    approved_decisions = 0
    changes_decisions = 0
    rejected_decisions = 0
    first_round_by_task: dict[str, str] = {}
    emp_trends: dict[str, dict] = {}

    for rev in all_reviews:
        if rev.decision == TaskReview.Decision.APPROVED:
            approved_decisions += 1
        elif rev.decision == TaskReview.Decision.CHANGES_REQUESTED:
            changes_decisions += 1
        elif rev.decision == TaskReview.Decision.REJECTED:
            rejected_decisions += 1

        tid = str(rev.task_id)
        if rev.review_round == 1:
            first_round_by_task[tid] = rev.decision

        rid = str(rev.reviewer_id)
        r_entry = reviewer_pending_map.setdefault(
            rid,
            {
                "reviewer_id": rid,
                "reviewer_name": rev.reviewer.full_name if rev.reviewer else "Unknown",
                "pending_count": 0,
                "approved_count": 0,
                "returned_count": 0,
            },
        )
        if rev.decision == TaskReview.Decision.APPROVED:
            r_entry["approved_count"] += 1
        else:
            r_entry["returned_count"] += 1

        if rev.task and rev.task.task_owner_id:
            oid = str(rev.task.task_owner_id)
            o_entry = emp_trends.setdefault(
                oid,
                {
                    "user_id": oid,
                    "user_name": rev.task.task_owner.full_name,
                    "total_review_events": 0,
                    "approved_events": 0,
                    "correction_or_rejection_events": 0,
                },
            )
            o_entry["total_review_events"] += 1
            if rev.decision == TaskReview.Decision.APPROVED:
                o_entry["approved_events"] += 1
            else:
                o_entry["correction_or_rejection_events"] += 1

    first_pass_total = len(first_round_by_task)
    first_pass_approved = sum(
        1 for d in first_round_by_task.values() if d == TaskReview.Decision.APPROVED
    )
    first_pass_rate = (
        round((first_pass_approved / first_pass_total) * 100.0, 1) if first_pass_total > 0 else 100.0
    )

    return {
        "pending_review_count": len(in_review_tasks),
        "changes_requested_count": changes_requested_count,
        "approved_ready_count": approved_ready_count,
        "ready_to_submit_count": ready_to_submit_count,
        "overdue_escalations_count": overdue_escalations_count,
        "total_reviews_logged": len(all_reviews),
        "approved_decisions_count": approved_decisions,
        "changes_requested_decisions_count": changes_decisions,
        "rejected_decisions_count": rejected_decisions,
        "first_pass_approval_rate": first_pass_rate,
        "rules": rules,
        "reviewer_workload": sorted(
            reviewer_pending_map.values(),
            key=lambda x: (x["pending_count"], x["approved_count"] + x["returned_count"]),
            reverse=True,
        ),
        "employee_correction_trends": sorted(
            emp_trends.values(),
            key=lambda x: x["correction_or_rejection_events"],
            reverse=True,
        ),
    }
