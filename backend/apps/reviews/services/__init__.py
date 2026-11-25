from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.companies.models import Company, CompanyMembership
from apps.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
)
from apps.organization.models import DepartmentMembership, TeamMembership
from apps.projects.models import Project
from apps.rbac.models import MembershipRole
from apps.reviews.models import TaskReview
from apps.reviews.permissions import (
    assert_can_reassign_reviewer,
    is_user_authorized_reviewer,
)
from apps.system_settings.models import SystemSetting
from apps.tasks.models import (
    Attachment,
    CommentAttachment,
    Task,
    TaskAssignment,
    TaskAttachment,
    TaskComment,
)
from apps.tasks.services import _validate_user_in_project_company


VALID_APPROVAL_LEVELS = (
    "Reviewer",
    "Team Leader",
    "Department Head",
    "Project Manager",
    "Company In-Charge",
)

DEFAULT_ESCALATION_HOURS = 48


def _create_notification_with_delivery(
    *,
    user_id,
    notification_type: str,
    title: str,
    message: str,
    related_task: Task | None = None,
    related_project: Project | None = None,
) -> Notification | None:
    if not user_id:
        return None
    pref = NotificationPreference.objects.filter(
        user_id=user_id, notification_type=notification_type
    ).first()
    in_app_enabled = True if pref is None else bool(pref.in_app_enabled)
    email_enabled = False if pref is None else bool(pref.email_enabled)

    if not in_app_enabled and not email_enabled:
        return None

    notif = Notification.objects.create(
        user_id=user_id,
        notification_type=notification_type,
        title=title[:200],
        message=message,
        related_task=related_task,
        related_project=related_project,
    )
    now_ts = timezone.now()
    if in_app_enabled:
        NotificationDelivery.objects.create(
            notification=notif,
            channel=NotificationDelivery.Channel.IN_APP,
            delivery_status=NotificationDelivery.DeliveryStatus.SENT,
            sent_at=now_ts,
        )
    if email_enabled:
        NotificationDelivery.objects.create(
            notification=notif,
            channel=NotificationDelivery.Channel.EMAIL,
            delivery_status=NotificationDelivery.DeliveryStatus.PENDING,
        )
    return notif


def get_approval_chain_rules(*, company: Company | None = None, project: Project | None = None, task: Task | None = None) -> dict:
    """
    SRS §14.1, §14.4 & §20.2:
    Resolves review/approval configuration with clear precedence:
    Task custom -> Project custom -> Company `approval_chain_rules` -> Global `approval_chain_rules` -> Defaults.
    """
    eff_project = project or (task.project if task else None)
    eff_company = company or (eff_project.company if eff_project else None)

    company_setting = None
    if eff_company is not None:
        company_setting = SystemSetting.objects.filter(
            company=eff_company, setting_key="approval_chain_rules"
        ).first()
    global_setting = SystemSetting.objects.filter(
        company__isnull=True, setting_key="approval_chain_rules"
    ).first()

    base_setting = company_setting or global_setting
    base_val = base_setting.setting_value if (base_setting and isinstance(base_setting.setting_value, dict)) else {}

    escalation_hours = DEFAULT_ESCALATION_HOURS
    if eff_company is not None:
        esc_setting = (
            SystemSetting.objects.filter(company=eff_company, setting_key="review_escalation_hours").first()
            or SystemSetting.objects.filter(company__isnull=True, setting_key="review_escalation_hours").first()
        )
    else:
        esc_setting = SystemSetting.objects.filter(
            company__isnull=True, setting_key="review_escalation_hours"
        ).first()

    raw_esc = base_val.get("escalation_hours")
    if raw_esc is None and esc_setting is not None:
        raw_esc = esc_setting.setting_value
    if raw_esc is not None:
        try:
            escalation_hours = max(0.01, float(raw_esc))
        except (TypeError, ValueError):
            escalation_hours = DEFAULT_ESCALATION_HOURS

    allow_reassignment = bool(base_val.get("allow_reviewer_reassignment", True))
    default_chain = base_val.get("default_chain") or ["Reviewer"]
    priority_chains = base_val.get("priority_chains") or {}

    chain_source = "Company Policy" if company_setting else ("Global Policy" if global_setting else "Default Single-Step")
    active_levels = list(priority_chains.get(task.priority) if (task and task.priority in priority_chains) else default_chain)
    user_overrides: dict = {}

    if eff_project is not None:
        proj_setting = SystemSetting.objects.filter(
            company=eff_project.company,
            setting_key=f"project_approval_chain:{eff_project.id}",
        ).first()
        if proj_setting and isinstance(proj_setting.setting_value, dict):
            pval = proj_setting.setting_value
            if pval.get("levels"):
                active_levels = list(pval["levels"])
                chain_source = "Project Policy"
            if isinstance(pval.get("user_overrides"), dict):
                user_overrides.update(pval["user_overrides"])
            if pval.get("escalation_hours") is not None:
                try:
                    escalation_hours = max(0.01, float(pval["escalation_hours"]))
                except (TypeError, ValueError):
                    pass

    if task is not None:
        task_setting = SystemSetting.objects.filter(
            company=task.project.company,
            setting_key=f"task_approval_chain:{task.id}",
        ).first()
        if task_setting and isinstance(task_setting.setting_value, dict):
            tval = task_setting.setting_value
            if tval.get("levels"):
                active_levels = list(tval["levels"])
                chain_source = "Task Custom"
            if isinstance(tval.get("user_overrides"), dict):
                user_overrides.update(tval["user_overrides"])
            if tval.get("escalation_hours") is not None:
                try:
                    escalation_hours = max(0.01, float(tval["escalation_hours"]))
                except (TypeError, ValueError):
                    pass

    return {
        "chain_source": chain_source,
        "levels": active_levels or ["Reviewer"],
        "default_chain": default_chain,
        "priority_chains": priority_chains,
        "user_overrides": user_overrides,
        "escalation_hours": escalation_hours,
        "allow_reviewer_reassignment": allow_reassignment,
    }


def _resolve_level_user(task: Task, level_name: str, step_index: int, user_overrides: dict) -> User | None:
    override_uid = user_overrides.get(str(step_index)) or user_overrides.get(level_name)
    if override_uid:
        u = User.objects.filter(id=override_uid, is_active=True).first()
        if u:
            return u

    if level_name == "Reviewer":
        return task.reviewer or task.project.project_manager

    if level_name == "Team Leader":
        team = task.team
        if team is None and task.task_owner_id:
            tm_owner = (
                TeamMembership.objects.filter(
                    user_id=task.task_owner_id,
                    team__department__company_id=task.project.company_id,
                    status=TeamMembership.Status.ACTIVE,
                )
                .select_related("team")
                .first()
            )
            if tm_owner:
                team = tm_owner.team
        if team is not None:
            leader_mem = (
                TeamMembership.objects.filter(
                    team=team,
                    is_leader=True,
                    status=TeamMembership.Status.ACTIVE,
                )
                .select_related("user")
                .first()
            )
            if leader_mem:
                return leader_mem.user
        return None

    if level_name == "Department Head":
        dept = task.department or (task.team.department if task.team_id else None)
        if dept is not None:
            head_mem = (
                DepartmentMembership.objects.filter(
                    department=dept,
                    is_head=True,
                    status=DepartmentMembership.Status.ACTIVE,
                )
                .select_related("user")
                .first()
            )
            if head_mem:
                return head_mem.user
        return None

    if level_name == "Project Manager":
        return task.project.project_manager

    if level_name == "Company In-Charge":
        incharge_mr = (
            MembershipRole.objects.filter(
                membership__company=task.project.company,
                membership__status=CompanyMembership.Status.ACTIVE,
                role__code__in=["COMPANY_INCHARGE", "IN_CHARGE", "CEO", "SUPER_ADMIN"],
            )
            .select_related("membership__user")
            .first()
        )
        if incharge_mr:
            return incharge_mr.membership.user
        return User.objects.filter(
            company_memberships__company=task.project.company,
            company_memberships__status=CompanyMembership.Status.ACTIVE,
            is_superuser=True,
        ).first()

    return task.reviewer or task.project.project_manager


def resolve_task_approval_chain(task: Task) -> dict:
    """
    SRS §14.1 & §14.3:
    Builds the live ordered approval steps for `task`, mapping current-cycle `TaskReview`
    approvals to steps so multi-level approvals (e.g. Employee -> Team Leader -> Project Manager)
    and rework/correction resets work deterministically.
    """
    rules = get_approval_chain_rules(task=task)
    raw_levels = rules["levels"]
    user_overrides = rules.get("user_overrides") or {}
    has_explicit_overrides = bool(user_overrides)

    resolved_candidates = []
    for idx, lvl in enumerate(raw_levels, start=1):
        approver = _resolve_level_user(task, lvl, idx, user_overrides)
        if approver is None:
            continue
        # Collapse duplicate consecutive approvers unless explicitly overridden per step
        if (
            not has_explicit_overrides
            and resolved_candidates
            and resolved_candidates[-1]["user_id"] == str(approver.id)
        ):
            continue
        resolved_candidates.append(
            {
                "role": lvl,
                "user": approver,
                "user_id": str(approver.id),
                "user_name": approver.full_name,
                "user_designation": approver.designation,
            }
        )

    if not resolved_candidates:
        fallback_user = task.reviewer or task.project.project_manager or task.created_by
        resolved_candidates.append(
            {
                "role": "Reviewer",
                "user": fallback_user,
                "user_id": str(fallback_user.id) if fallback_user else None,
                "user_name": fallback_user.full_name if fallback_user else "Unassigned",
                "user_designation": fallback_user.designation if fallback_user else None,
            }
        )

    all_reviews = list(
        task.reviews.select_related("reviewer").order_by("review_round", "reviewed_at")
    )

    # Respect approval cycle resets triggered by reopening a Completed task (SRS §14.3)
    latest_reopen_reset = (
        TaskComment.objects.filter(
            task=task, content__startswith="[Approval Cycle Reset"
        )
        .order_by("-created_at")
        .first()
    )
    reset_ts = latest_reopen_reset.created_at if latest_reopen_reset else None

    last_reset_idx = -1
    for i, rev in enumerate(all_reviews):
        if reset_ts and rev.reviewed_at <= reset_ts:
            last_reset_idx = i
            continue
        if rev.decision in (
            TaskReview.Decision.CHANGES_REQUESTED,
            TaskReview.Decision.REJECTED,
        ):
            last_reset_idx = i

    current_cycle_reviews = all_reviews[last_reset_idx + 1 :]
    approved_in_cycle = [
        rev for rev in current_cycle_reviews if rev.decision == TaskReview.Decision.APPROVED
    ]

    completed_count = min(len(approved_in_cycle), len(resolved_candidates))

    steps = []
    current_step = None
    next_step = None
    for idx, cand in enumerate(resolved_candidates):
        step_num = idx + 1
        if idx < completed_count:
            rev_obj = approved_in_cycle[idx]
            step_data = {
                "step_number": step_num,
                "role": cand["role"],
                "user_id": str(rev_obj.reviewer_id),
                "user_name": rev_obj.reviewer.full_name if rev_obj.reviewer else cand["user_name"],
                "user_designation": cand["user_designation"],
                "status": "Approved",
                "review_id": str(rev_obj.id),
                "review_round": rev_obj.review_round,
                "approved_at": rev_obj.reviewed_at,
                "comments": rev_obj.comments,
            }
        elif idx == completed_count:
            step_data = {
                "step_number": step_num,
                "role": cand["role"],
                "user_id": cand["user_id"],
                "user_name": cand["user_name"],
                "user_designation": cand["user_designation"],
                "status": "Pending" if task.status == Task.Status.IN_REVIEW else "Awaiting Submission",
                "review_id": None,
                "review_round": None,
                "approved_at": None,
                "comments": None,
            }
            if task.status == Task.Status.APPROVED:
                step_data["status"] = "Approved"
            current_step = step_data
        else:
            step_data = {
                "step_number": step_num,
                "role": cand["role"],
                "user_id": cand["user_id"],
                "user_name": cand["user_name"],
                "user_designation": cand["user_designation"],
                "status": "Approved" if task.status == Task.Status.APPROVED else "Waiting",
                "review_id": None,
                "review_round": None,
                "approved_at": None,
                "comments": None,
            }
            if next_step is None and idx == completed_count + 1:
                next_step = step_data
        steps.append(step_data)

    is_chain_satisfied = completed_count >= len(resolved_candidates) and len(approved_in_cycle) > 0
    is_approval_satisfied = is_chain_satisfied or (task.status == Task.Status.APPROVED)

    return {
        "chain_source": rules["chain_source"],
        "levels": [s["role"] for s in steps],
        "total_steps": len(steps),
        "completed_steps": len(steps) if task.status == Task.Status.APPROVED else completed_count,
        "current_step_number": None if is_approval_satisfied else (completed_count + 1),
        "current_step_role": None if is_approval_satisfied else (current_step["role"] if current_step else None),
        "current_reviewer_id": None if is_approval_satisfied else (current_step["user_id"] if current_step else None),
        "current_reviewer_name": None if is_approval_satisfied else (current_step["user_name"] if current_step else None),
        "is_chain_satisfied": is_chain_satisfied,
        "is_approval_satisfied": is_approval_satisfied,
        "escalation_hours": rules["escalation_hours"],
        "allow_reviewer_reassignment": rules["allow_reviewer_reassignment"],
        "steps": steps,
    }


def is_task_approval_satisfied(task: Task) -> bool:
    """
    SRS §14.3 & §22 #7:
    Returns True if the task does not require approval OR its approval requirements
    are currently satisfied (status is Approved OR all active chain steps are approved).
    """
    if not task.approval_required:
        return True
    if task.status == Task.Status.APPROVED:
        return True
    chain = resolve_task_approval_chain(task)
    return bool(chain["is_chain_satisfied"])


def _link_review_attachments(
    *,
    task: Task,
    reviewer: User,
    decision: str,
    review_round: int,
    comments: str | None,
    correction_reason: str | None,
    attachment_ids: list | None = None,
    uploaded_files: list | None = None,
) -> tuple[TaskComment, list[Attachment]]:
    """
    Creates a traceable TaskComment for the review round and links any uploaded or
    existing reference documents using explicit `task_attachments` & `comment_attachments`
    tables (DB Design §4.5, SRS §14.2).
    """
    from apps.tasks.services.attachments import store_and_create_attachment_service

    parts = [f"[Review Round #{review_round} — {decision}]"]
    if comments and comments.strip():
        parts.append(comments.strip())
    if correction_reason and correction_reason.strip():
        parts.append(f"Correction Reason: {correction_reason.strip()}")

    review_comment = TaskComment.objects.create(
        task=task,
        author=reviewer,
        content=" | ".join(parts),
    )

    linked_attachments: list[Attachment] = []
    if attachment_ids:
        unique_ids = [str(aid) for aid in dict.fromkeys(attachment_ids) if aid]
        for aid in unique_ids:
            att = Attachment.objects.filter(id=aid).first()
            if att:
                TaskAttachment.objects.get_or_create(task=task, attachment=att)
                CommentAttachment.objects.get_or_create(comment=review_comment, attachment=att)
                linked_attachments.append(att)

    if uploaded_files:
        for uf in uploaded_files:
            att = store_and_create_attachment_service(
                uploaded_by=reviewer,
                uploaded_file=uf,
                task=task,
                comment=review_comment,
            )
            linked_attachments.append(att)

    return review_comment, linked_attachments


@transaction.atomic
def configure_approval_chain_service(
    *,
    scope: str,
    levels: list[str],
    actor: User | None = None,
    company: Company | None = None,
    project: Project | None = None,
    task: Task | None = None,
    priority_chains: dict | None = None,
    user_overrides: dict | None = None,
    escalation_hours: float | None = None,
    allow_reviewer_reassignment: bool | None = None,
) -> dict:
    """
    SRS §14.1, §14.4 & §20.2:
    Persists configurable approval chains and escalation intervals in `system_settings`.
    """
    if not levels or not isinstance(levels, list):
        raise ValidationError("Provide at least one approval level in `levels`.")
    invalid = [lvl for lvl in levels if lvl not in VALID_APPROVAL_LEVELS]
    if invalid:
        raise ValidationError(
            f"Unsupported approval level(s): {invalid}. Valid options: {list(VALID_APPROVAL_LEVELS)}."
        )

    if scope == "task":
        if not task:
            raise ValidationError("A valid `task` is required for task-scoped chain configuration.")
        eff_company = task.project.company
        if user_overrides:
            for _, uid in user_overrides.items():
                if uid:
                    u = User.objects.filter(id=uid).first()
                    if not u:
                        raise ValidationError("Selected override approver was not found.")
                    _validate_user_in_project_company(u, eff_company, label="Chain approver")
        payload = {
            "levels": levels,
            "user_overrides": user_overrides or {},
        }
        if escalation_hours is not None:
            payload["escalation_hours"] = max(0.01, float(escalation_hours))
        SystemSetting.objects.update_or_create(
            company=eff_company,
            setting_key=f"task_approval_chain:{task.id}",
            defaults={
                "setting_value": payload,
                "updated_by": actor if getattr(actor, "id", None) else None,
            },
        )
        chain_info = resolve_task_approval_chain(task)
        if not chain_info["is_approval_satisfied"] and chain_info.get("current_reviewer_id"):
            task.reviewer_id = chain_info["current_reviewer_id"]
            task.save(update_fields=["reviewer", "updated_at"])
        return {"scope": "task", "task_id": str(task.id), "approval_chain": chain_info}

    if scope == "project":
        if not project:
            raise ValidationError("A valid `project` is required for project-scoped chain configuration.")
        payload = {
            "levels": levels,
            "user_overrides": user_overrides or {},
        }
        if escalation_hours is not None:
            payload["escalation_hours"] = max(0.01, float(escalation_hours))
        SystemSetting.objects.update_or_create(
            company=project.company,
            setting_key=f"project_approval_chain:{project.id}",
            defaults={
                "setting_value": payload,
                "updated_by": actor if getattr(actor, "id", None) else None,
            },
        )
        return {
            "scope": "project",
            "project_id": str(project.id),
            "rules": get_approval_chain_rules(project=project),
        }

    # Company or Global scope
    eff_company = company or (project.company if project else (task.project.company if task else None))
    if priority_chains:
        for prio, p_levels in priority_chains.items():
            if not isinstance(p_levels, list) or not p_levels:
                raise ValidationError(f"Priority '{prio}' chain must be a non-empty list of levels.")
            bad_lvls = [l for l in p_levels if l not in VALID_APPROVAL_LEVELS]
            if bad_lvls:
                raise ValidationError(f"Invalid level(s) {bad_lvls} for priority '{prio}'.")

    existing = SystemSetting.objects.filter(
        company=eff_company, setting_key="approval_chain_rules"
    ).first()
    existing_val = dict(existing.setting_value) if (existing and isinstance(existing.setting_value, dict)) else {}

    new_val = {
        "default_chain": levels,
        "priority_chains": priority_chains if priority_chains is not None else existing_val.get("priority_chains", {}),
        "escalation_hours": (
            max(0.01, float(escalation_hours))
            if escalation_hours is not None
            else existing_val.get("escalation_hours", DEFAULT_ESCALATION_HOURS)
        ),
        "allow_reviewer_reassignment": (
            bool(allow_reviewer_reassignment)
            if allow_reviewer_reassignment is not None
            else existing_val.get("allow_reviewer_reassignment", True)
        ),
    }

    if existing:
        existing.setting_value = new_val
        existing.updated_by = actor if getattr(actor, "id", None) else existing.updated_by
        existing.save()
    else:
        SystemSetting.objects.create(
            company=eff_company,
            setting_key="approval_chain_rules",
            setting_value=new_val,
            updated_by=actor if getattr(actor, "id", None) else None,
        )

    return {
        "scope": "company" if eff_company else "global",
        "company_id": str(eff_company.id) if eff_company else None,
        "rules": get_approval_chain_rules(company=eff_company),
    }


@transaction.atomic
def submit_for_review_service(
    *,
    task: Task,
    actor: User | None = None,
    reviewer_id=None,
    notes: str | None = None,
    attachment_ids: list | None = None,
    uploaded_files: list | None = None,
    approval_required: bool | None = None,
    override_dependencies: bool = False,
    override_reason: str | None = None,
) -> Task:
    """
    SRS §2.6, §7.3, §7.9, §13.1 & §14.1:
    Transitions an eligible task to `In Review`, enforces prerequisite dependency rules
    (with optional authorized override), assigns the active pending chain reviewer
    (or requested same-company reviewer), attaches submission deliverables,
    and notifies the reviewer.
    """
    eligible_statuses = {
        Task.Status.TO_DO,
        Task.Status.ASSIGNED,
        Task.Status.IN_PROGRESS,
        Task.Status.CHANGES_REQUESTED,
        Task.Status.IN_REVIEW,
    }
    if task.status not in eligible_statuses:
        raise ValidationError(
            f"Cannot submit a '{task.status}' task for review. Move the task to In Progress (or resolve Blocked/On Hold status) first (SRS §7.3 & §14.1)."
        )

    has_active_assign = task.assignments.filter(status__in=["Active", "Acknowledged"]).exists()
    if not task.task_owner_id and not has_active_assign:
        raise ValidationError(
            "Task must have a responsible Task Owner or active assignee before submission for review (SRS §22 #3)."
        )

    # SRS §7.9 & §22 #20: Enforce prerequisite task dependencies on submission for review
    from apps.tasks.services.dependencies import check_dependency_rules_for_transition

    check_dependency_rules_for_transition(
        task=task,
        new_status=Task.Status.IN_REVIEW,
        override_dependencies=override_dependencies,
        override_reason=override_reason,
    )

    company = task.project.company
    if actor:
        _validate_user_in_project_company(actor, company, label="Submitter")

    if approval_required is not None:
        task.approval_required = bool(approval_required)

    # Resolve target reviewer without accidentally rewinding mid-chain progress
    target_reviewer = None
    if reviewer_id:
        target_reviewer = User.objects.filter(id=reviewer_id, is_active=True).first()
        if not target_reviewer:
            raise ValidationError("Selected reviewer user was not found or is inactive.")
        _validate_user_in_project_company(target_reviewer, company, label="Reviewer")
        task.reviewer = target_reviewer
    else:
        chain_preview = resolve_task_approval_chain(task)
        active_step_uid = chain_preview.get("current_reviewer_id") or (
            chain_preview["steps"][0]["user_id"] if chain_preview["steps"] else None
        )
        if not task.reviewer_id and active_step_uid:
            task.reviewer_id = active_step_uid
        elif chain_preview["total_steps"] > 1 and active_step_uid:
            # Align to current pending step (Step 1 on fresh/rework, or next pending step if updating mid-review)
            task.reviewer_id = active_step_uid
        target_reviewer = task.reviewer

    old_status = task.status
    task.status = Task.Status.IN_REVIEW
    task.save()

    sub_actor = actor or task.task_owner or task.created_by
    sub_comment = TaskComment.objects.create(
        task=task,
        author=sub_actor,
        content=(
            f"[Submitted for Review] {old_status} → In Review"
            + (f" | Assigned Reviewer: {target_reviewer.full_name}" if target_reviewer else "")
            + (f" | Notes: {notes.strip()}" if notes and notes.strip() else "")
            + (
                f" | Dependency Override: {override_reason.strip()}"
                if override_dependencies and override_reason and override_reason.strip()
                else ""
            )
        ),
    )

    if attachment_ids:
        for aid in [str(a) for a in dict.fromkeys(attachment_ids) if a]:
            att = Attachment.objects.filter(id=aid).first()
            if att:
                TaskAttachment.objects.get_or_create(task=task, attachment=att)
                CommentAttachment.objects.get_or_create(comment=sub_comment, attachment=att)

    if uploaded_files:
        from apps.tasks.services.attachments import store_and_create_attachment_service

        for uf in uploaded_files:
            store_and_create_attachment_service(
                uploaded_by=sub_actor,
                uploaded_file=uf,
                task=task,
                comment=sub_comment,
            )

    if target_reviewer:
        _create_notification_with_delivery(
            user_id=target_reviewer.id,
            notification_type="REVIEW_REQUESTED",
            title=f"Review Requested: {task.title}",
            message=f"{sub_actor.full_name} submitted task '{task.title}' ({task.project.code}) for your review.",
            related_task=task,
            related_project=task.project,
        )

    return task


@transaction.atomic
def submit_review_decision_service(
    *,
    task: Task,
    reviewer: User,
    decision: str,
    comments: str | None = None,
    correction_reason: str | None = None,
    attachment_ids: list | None = None,
    uploaded_files: list | None = None,
    enforce_actor_permission: bool = False,
) -> TaskReview:
    """
    SRS §14.1, §14.2, §14.3, §13.1 & DB Design §4.6, §5, §7:
    Records an immutable `TaskReview` decision row inside `transaction.atomic()`,
    attaches optional reference documents via explicit attachment tables,
    advances multi-level approval chains or marks task Approved / Changes Requested,
    and dispatches notifications.
    """
    if not reviewer:
        raise ValidationError("A reviewer user is required (DB Design §4.6 & §5).")

    if task.status in (Task.Status.COMPLETED, Task.Status.CANCELLED, Task.Status.BLOCKED):
        raise ValidationError(
            f"Cannot record a review decision on a '{task.status}' task. Reopen or unblock the task and submit it for review first (SRS §7.3 & §14.2)."
        )

    if enforce_actor_permission and task.status not in (
        Task.Status.IN_REVIEW,
        Task.Status.APPROVED,
    ):
        raise ValidationError(
            f"Task '{task.title}' is currently '{task.status}'. Submit the task for review ('In Review') before recording a review decision (SRS §14.1 & §14.2)."
        )

    valid_decisions = {c[0] for c in TaskReview.Decision.choices}
    if decision not in valid_decisions:
        raise ValidationError(f"Invalid review decision '{decision}'. Allowed: {sorted(valid_decisions)}.")

    if decision in (TaskReview.Decision.CHANGES_REQUESTED, TaskReview.Decision.REJECTED):
        if not (correction_reason and str(correction_reason).strip()):
            raise ValidationError(
                "Correction reason is required when requesting changes or rejecting (DB Design §5 & SRS §14.2)."
            )

    company = task.project.company
    _validate_user_in_project_company(reviewer, company, label="Reviewer")

    chain_before = resolve_task_approval_chain(task)
    if (
        decision == TaskReview.Decision.APPROVED
        and task.status == Task.Status.APPROVED
        and chain_before["is_chain_satisfied"]
    ):
        raise ValidationError(
            "Task is already Approved and all approval chain steps are satisfied (SRS §14.3)."
        )
    chain_user_ids = {s["user_id"] for s in chain_before["steps"] if s.get("user_id")}

    if enforce_actor_permission:
        if not is_user_authorized_reviewer(task, reviewer, extra_chain_user_ids=chain_user_ids):
            raise PermissionDenied(
                f"User '{reviewer.full_name}' is not authorized to review or approve task '{task.title}' (SRS §14.2 & §27.5)."
            )

        # Enforce sequential step ordering on multi-level approval chains when approving
        if (
            decision == TaskReview.Decision.APPROVED
            and chain_before["total_steps"] > 1
            and chain_before.get("current_reviewer_id")
            and str(reviewer.id) != str(chain_before["current_reviewer_id"])
            and not getattr(reviewer, "is_superuser", False)
            and reviewer.id != task.reviewer_id
        ):
            raise PermissionDenied(
                f"Multi-level approval chain active (Step {chain_before['current_step_number']}/{chain_before['total_steps']}): "
                f"currently awaiting '{chain_before['current_reviewer_name']}' ({chain_before['current_step_role']}). "
                f"Reassign reviewer first if delegating (SRS §14.1 & §14.2)."
            )

    next_round = task.reviews.count() + 1
    review = TaskReview.objects.create(
        task=task,
        reviewer=reviewer,
        decision=decision,
        comments=(comments or "").strip() or None,
        correction_reason=(correction_reason or "").strip() or None,
        review_round=next_round,
    )

    _link_review_attachments(
        task=task,
        reviewer=reviewer,
        decision=decision,
        review_round=next_round,
        comments=comments,
        correction_reason=correction_reason,
        attachment_ids=attachment_ids,
        uploaded_files=uploaded_files,
    )

    # Determine recipients for notifications (task_owner + active user assignees)
    assignee_ids = set(
        TaskAssignment.objects.filter(
            task=task,
            assignment_type=TaskAssignment.AssignmentType.USER,
            status__in=[TaskAssignment.Status.ACTIVE, TaskAssignment.Status.ACKNOWLEDGED],
            user_id__isnull=False,
        ).values_list("user_id", flat=True)
    )
    if task.task_owner_id:
        assignee_ids.add(task.task_owner_id)
    recipient_ids = {uid for uid in assignee_ids if uid and uid != reviewer.id}

    if decision == TaskReview.Decision.APPROVED:
        chain_after = resolve_task_approval_chain(task)
        if chain_after["is_chain_satisfied"]:
            task.status = Task.Status.APPROVED
            task.save(update_fields=["status", "updated_at"])
            for uid in recipient_ids:
                _create_notification_with_delivery(
                    user_id=uid,
                    notification_type="TASK_APPROVED",
                    title=f"Task Approved: {task.title}",
                    message=(
                        f"'{task.title}' passed final review (Round #{next_round}) by {reviewer.full_name} "
                        f"and is now Approved."
                    ),
                    related_task=task,
                    related_project=task.project,
                )
        else:
            # Multi-level chain: advance to next pending approval stage (SRS §14.1)
            next_uid = chain_after.get("current_reviewer_id")
            next_name = chain_after.get("current_reviewer_name")
            next_role = chain_after.get("current_step_role")
            task.status = Task.Status.IN_REVIEW
            update_fields = ["status", "updated_at"]
            if next_uid:
                task.reviewer_id = next_uid
                update_fields.append("reviewer")
            task.save(update_fields=update_fields)

            if next_uid:
                _create_notification_with_delivery(
                    user_id=next_uid,
                    notification_type="REVIEW_REQUESTED",
                    title=f"Next-Level Review Required ({next_role}): {task.title}",
                    message=(
                        f"Round #{next_round} approved by {reviewer.full_name}. "
                        f"Step {chain_after['current_step_number']}/{chain_after['total_steps']} ({next_role}) is now assigned to you."
                    ),
                    related_task=task,
                    related_project=task.project,
                )
            for uid in recipient_ids:
                _create_notification_with_delivery(
                    user_id=uid,
                    notification_type="REVIEW_STEP_APPROVED",
                    title=f"Review Step Approved: {task.title}",
                    message=(
                        f"Step approved by {reviewer.full_name} ({chain_after['completed_steps']}/{chain_after['total_steps']}). "
                        f"Forwarded to {next_name or 'next approver'} ({next_role})."
                    ),
                    related_task=task,
                    related_project=task.project,
                )
    else:
        # Changes Requested or Rejected -> return to Changes Requested (SRS §7.3 & §14.2)
        task.status = Task.Status.CHANGES_REQUESTED
        task.save(update_fields=["status", "updated_at"])

        notif_type = (
            "REVIEW_REJECTED"
            if decision == TaskReview.Decision.REJECTED
            else "CHANGES_REQUESTED"
        )
        action_label = "Rejected" if decision == TaskReview.Decision.REJECTED else "Changes Requested"
        for uid in recipient_ids:
            _create_notification_with_delivery(
                user_id=uid,
                notification_type=notif_type,
                title=f"{action_label}: {task.title}",
                message=(
                    f"{reviewer.full_name} marked '{task.title}' as {decision} (Round #{next_round}). "
                    f"Reason: {review.correction_reason}"
                ),
                related_task=task,
                related_project=task.project,
            )

    from apps.projects.services import recalculate_project_progress_service

    recalculate_project_progress_service(project=task.project)
    return review


@transaction.atomic
def reassign_reviewer_service(
    *,
    task: Task,
    new_reviewer_id,
    actor: User | None = None,
    reason: str | None = None,
    enforce_actor_permission: bool = False,
) -> Task:
    """
    SRS §14.2 & DB Design §5:
    Reassigns the active reviewer when permitted, validates company membership, updates
    any active custom chain step override, logs a traceable comment, and notifies parties.
    """
    if not new_reviewer_id:
        raise ValidationError("A valid `new_reviewer` user ID is required.")

    new_reviewer = User.objects.filter(id=new_reviewer_id, is_active=True).first()
    if not new_reviewer:
        raise ValidationError("Selected new reviewer user was not found or is inactive.")

    company = task.project.company
    _validate_user_in_project_company(new_reviewer, company, label="New reviewer")

    rules = get_approval_chain_rules(task=task)
    if enforce_actor_permission and actor is not None:
        assert_can_reassign_reviewer(
            task, actor, allow_reassignment_policy=rules["allow_reviewer_reassignment"]
        )

    old_reviewer = task.reviewer
    chain_before = resolve_task_approval_chain(task)

    task.reviewer = new_reviewer
    task.save(update_fields=["reviewer", "updated_at"])

    # Pin the current pending approval chain step to `new_reviewer` even when using
    # a Company or Project policy chain, so the stepper & sequential check stay in sync!
    curr_step_num = chain_before.get("current_step_number")
    if curr_step_num and not chain_before.get("is_approval_satisfied"):
        task_setting = SystemSetting.objects.filter(
            company=company, setting_key=f"task_approval_chain:{task.id}"
        ).first()
        if task_setting and isinstance(task_setting.setting_value, dict):
            s_val = dict(task_setting.setting_value)
            overrides = dict(s_val.get("user_overrides") or {})
            overrides[str(curr_step_num)] = str(new_reviewer.id)
            s_val["user_overrides"] = overrides
            task_setting.setting_value = s_val
            task_setting.updated_by = actor if getattr(actor, "id", None) else task_setting.updated_by
            task_setting.save()
        elif chain_before["total_steps"] > 1 or chain_before.get("current_reviewer_id") != str(new_reviewer.id):
            overrides = dict(rules.get("user_overrides") or {})
            overrides[str(curr_step_num)] = str(new_reviewer.id)
            SystemSetting.objects.update_or_create(
                company=company,
                setting_key=f"task_approval_chain:{task.id}",
                defaults={
                    "setting_value": {
                        "levels": list(rules["levels"]),
                        "user_overrides": overrides,
                        "escalation_hours": rules["escalation_hours"],
                    },
                    "updated_by": actor if getattr(actor, "id", None) else None,
                },
            )

    comment_author = actor or task.project.project_manager or task.created_by
    reason_part = f" | Reason: {reason.strip()}" if reason and reason.strip() else ""
    TaskComment.objects.create(
        task=task,
        author=comment_author,
        content=(
            f"[Reviewer Reassigned] {old_reviewer.full_name if old_reviewer else 'Unassigned'} "
            f"→ {new_reviewer.full_name}{reason_part}"
        ),
    )

    _create_notification_with_delivery(
        user_id=new_reviewer.id,
        notification_type="REVIEWER_REASSIGNED",
        title=f"Assigned as Reviewer: {task.title}",
        message=(
            f"You have been assigned as reviewer for task '{task.title}' ({task.project.code})."
            + (f" Note: {reason.strip()}" if reason and reason.strip() else "")
        ),
        related_task=task,
        related_project=task.project,
    )

    if task.task_owner_id and task.task_owner_id != new_reviewer.id:
        _create_notification_with_delivery(
            user_id=task.task_owner_id,
            notification_type="REVIEWER_REASSIGNED",
            title=f"Task Reviewer Updated: {task.title}",
            message=f"Reviewer for '{task.title}' changed to {new_reviewer.full_name}.",
            related_task=task,
            related_project=task.project,
        )

    return task


def compute_task_review_sla(task: Task, threshold_hours: float | None = None) -> dict:
    """
    SRS §14.4 Review Escalation:
    Computes how long a task has been awaiting review in `In Review` status compared to
    the configured escalation interval (`escalation_hours`).
    """
    if threshold_hours is None:
        rules = get_approval_chain_rules(task=task)
        threshold_hours = float(rules["escalation_hours"])

    if task.status != Task.Status.IN_REVIEW:
        return {
            "submitted_for_review_at": None,
            "hours_awaiting_review": 0.0,
            "escalation_threshold_hours": threshold_hours,
            "is_review_overdue": False,
        }

    # Find timestamp of latest submission / review transition / previous step approval
    latest_sub_comment = (
        TaskComment.objects.filter(task=task)
        .filter(
            Q(content__startswith="[Submitted for Review]")
            | (Q(content__startswith="[Status Transition]") & Q(content__icontains="→ In Review"))
        )
        .order_by("-created_at")
        .first()
    )
    latest_rev = task.reviews.order_by("-reviewed_at").first()

    candidates = [task.updated_at or task.created_at]
    if latest_sub_comment:
        candidates.append(latest_sub_comment.created_at)
    if latest_rev and latest_rev.decision == TaskReview.Decision.APPROVED:
        candidates.append(latest_rev.reviewed_at)

    # Prefer the most recent submission/intermediate-step timestamp
    since_ts = max(candidates[1:]) if len(candidates) > 1 else candidates[0]
    elapsed_hours = max(0.0, (timezone.now() - since_ts).total_seconds() / 3600.0)

    return {
        "submitted_for_review_at": since_ts,
        "hours_awaiting_review": round(elapsed_hours, 2),
        "escalation_threshold_hours": round(float(threshold_hours), 2),
        "is_review_overdue": elapsed_hours >= float(threshold_hours),
    }


@transaction.atomic
def check_and_escalate_overdue_reviews_service(
    *,
    company_id=None,
    project_id=None,
    threshold_hours_override: float | None = None,
    trigger_notifications: bool = True,
    force_notify: bool = False,
) -> dict:
    """
    SRS §14.4 Review Escalation:
    Scans tasks awaiting review beyond the configured SLA interval and sends reminders
    (`REVIEW_ESCALATION`) to responsible reviewers and project managers.
    """
    qs = Task.objects.filter(status=Task.Status.IN_REVIEW).select_related(
        "project", "project__company", "project__project_manager", "reviewer", "task_owner"
    )
    if company_id:
        qs = qs.filter(project__company_id=company_id)
    if project_id:
        qs = qs.filter(project_id=project_id)

    checked_tasks = list(qs)
    overdue_items = []
    notifications_sent = 0
    cutoff = timezone.now() - timedelta(hours=12)

    for task in checked_tasks:
        sla = compute_task_review_sla(task, threshold_hours=threshold_hours_override)
        if not sla["is_review_overdue"]:
            continue

        notified_users = []
        if trigger_notifications:
            target_uids = {
                uid for uid in (task.reviewer_id, task.project.project_manager_id) if uid
            }
            for uid in target_uids:
                if not force_notify:
                    already_sent = Notification.objects.filter(
                        user_id=uid,
                        notification_type="REVIEW_ESCALATION",
                        related_task=task,
                        created_at__gte=cutoff,
                    ).exists()
                    if already_sent:
                        continue

                n = _create_notification_with_delivery(
                    user_id=uid,
                    notification_type="REVIEW_ESCALATION",
                    title=f"SLA Escalation — Review Overdue: {task.title}",
                    message=(
                        f"Task '{task.title}' ({task.project.code}) has been awaiting review for "
                        f"{sla['hours_awaiting_review']:.1f}h (SLA threshold: {sla['escalation_threshold_hours']}h). "
                        f"Current reviewer: {task.reviewer.full_name if task.reviewer else 'Unassigned'}."
                    ),
                    related_task=task,
                    related_project=task.project,
                )
                if n:
                    notifications_sent += 1
                    notified_users.append(str(uid))

        overdue_items.append(
            {
                "task_id": str(task.id),
                "task_title": task.title,
                "project_code": task.project.code,
                "reviewer_name": task.reviewer.full_name if task.reviewer else None,
                "hours_awaiting_review": sla["hours_awaiting_review"],
                "escalation_threshold_hours": sla["escalation_threshold_hours"],
                "notified_user_ids": notified_users,
            }
        )

    return {
        "checked_count": len(checked_tasks),
        "overdue_count": len(overdue_items),
        "notifications_sent": notifications_sent,
        "escalated_tasks": overdue_items,
    }
