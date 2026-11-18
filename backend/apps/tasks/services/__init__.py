from datetime import date
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import User
from apps.companies.models import CompanyMembership
from apps.notifications.models import Notification
from apps.organization.models import Team, TeamMembership
from apps.projects.models import ProjectMember
from apps.tasks.models import (
    Checklist,
    ChecklistItem,
    Task,
    TaskAssignment,
    TaskComment,
    TaskDependency,
    TaskTag,
    TaskTagAssignment,
)


def _validate_user_in_project_company(user: User, company, label: str = "User"):
    if getattr(user, "is_superuser", False) or getattr(company, "company_incharge_id", None) == user.id:
        return
    has_any = (
        CompanyMembership.objects.filter(company=company).exists()
        or CompanyMembership.objects.filter(user=user).exists()
    )
    if not has_any:
        return
    is_member = CompanyMembership.objects.filter(
        company=company,
        user=user,
        status=CompanyMembership.Status.ACTIVE,
    ).exists()
    if not is_member:
        raise ValidationError(
            f"{label} '{user.full_name}' does not belong to company '{company.code}' (DB Design §5)."
        )


def _validate_team_in_project_company(team: Team, company):
    if team.department.company_id != company.id:
        raise ValidationError(
            f"Team '{team.name}' does not belong to company '{company.code}' (DB Design §5)."
        )


@transaction.atomic
def assign_task_service(
    *,
    task: Task,
    assignment_type: str = TaskAssignment.AssignmentType.USER,
    user_id=None,
    team_id=None,
    assigned_by_id=None,
    replace_existing: bool = False,
    task_owner_id=None,
    reviewer_id=None,
    entire_team_responsible: bool = False,
) -> TaskAssignment:
    """
    SRS §7.2 & §22 Rules #3, #4, #11 & DB Design §4.4, §5, §7:
    Atomically assigns a task to a User or Team while validating company permissions,
    preserving assignment history on reassignment, handling team vs. individual task owner
    selection, and notifying assignees.
    """
    company = task.project.company
    assigner_id = assigned_by_id or task.created_by_id

    target_user = None
    target_team = None
    if assignment_type == TaskAssignment.AssignmentType.USER:
        if not user_id:
            raise ValidationError("A valid user must be provided when assignment_type is 'User'.")
        target_user = User.objects.filter(id=user_id).first()
        if not target_user:
            raise ValidationError("Selected assignee user was not found.")
        _validate_user_in_project_company(target_user, company, label="Assignee user")
    elif assignment_type == TaskAssignment.AssignmentType.TEAM:
        if not team_id:
            raise ValidationError("A valid team must be provided when assignment_type is 'Team'.")
        target_team = Team.objects.select_related("department").filter(id=team_id).first()
        if not target_team:
            raise ValidationError("Selected assignee team was not found.")
        _validate_team_in_project_company(target_team, company)
    else:
        raise ValidationError(f"Unsupported assignment_type '{assignment_type}'.")

    if replace_existing:
        for active in task.assignments.filter(status__in=["Active", "Acknowledged"]):
            # Do not end the exact same target if it's already active; we'll reuse or keep it
            if target_user and active.user_id == target_user.id:
                continue
            if target_team and active.team_id == target_team.id:
                continue
            active.end_assignment(status=TaskAssignment.Status.REASSIGNED)

    # Check if an active assignment for the same target already exists
    existing_active = None
    if target_user:
        existing_active = task.assignments.filter(
            assignment_type=TaskAssignment.AssignmentType.USER,
            user=target_user,
            status__in=["Active", "Acknowledged"],
        ).first()
    elif target_team:
        existing_active = task.assignments.filter(
            assignment_type=TaskAssignment.AssignmentType.TEAM,
            team=target_team,
            status__in=["Active", "Acknowledged"],
        ).first()

    if existing_active:
        assignment = existing_active
    else:
        assignment = TaskAssignment(
            task=task,
            assignment_type=assignment_type,
            user=target_user,
            team=target_team,
            assigned_by_id=assigner_id,
            status=TaskAssignment.Status.ACTIVE,
        )
        assignment.full_clean()
        assignment.save()

    # Update task_owner / reviewer / team on task if requested (SRS §7.2)
    task_update_fields = []
    if task_owner_id:
        owner_user = User.objects.filter(id=task_owner_id).first()
        if not owner_user:
            raise ValidationError("Selected task owner was not found.")
        _validate_user_in_project_company(owner_user, company, label="Task owner")
        task.task_owner = owner_user
        task_update_fields.append("task_owner")
    elif target_user and (replace_existing or not task.task_owner_id):
        task.task_owner = target_user
        task_update_fields.append("task_owner")
    elif target_team and not entire_team_responsible and not task.task_owner_id:
        # SRS §7.2: If team-assigned and individual owner not yet set, coordinate Team Leader as default owner
        leader_mem = (
            TeamMembership.objects.filter(
                team=target_team,
                is_leader=True,
                status=TeamMembership.Status.ACTIVE,
            )
            .select_related("user")
            .first()
        )
        if leader_mem:
            task.task_owner = leader_mem.user
            task_update_fields.append("task_owner")

    if target_team and not task.team_id:
        task.team = target_team
        task_update_fields.append("team")
        if not task.department_id:
            task.department_id = target_team.department_id
            task_update_fields.append("department")

    if reviewer_id:
        rev_user = User.objects.filter(id=reviewer_id).first()
        if not rev_user:
            raise ValidationError("Selected reviewer was not found.")
        _validate_user_in_project_company(rev_user, company, label="Reviewer")
        task.reviewer = rev_user
        task_update_fields.append("reviewer")

    if task.status in [Task.Status.BACKLOG, Task.Status.TO_DO]:
        task.status = Task.Status.ASSIGNED
        task_update_fields.append("status")

    if task_update_fields:
        task_update_fields.append("updated_at")
        task.save(update_fields=list(set(task_update_fields)))

    # Ensure assigned user is an active ProjectMember participant on the task's project
    if target_user:
        ProjectMember.objects.update_or_create(
            project=task.project,
            user=target_user,
            defaults={"status": ProjectMember.Status.ACTIVE, "left_at": None},
        )
        if not existing_active:
            notif_type = "TASK_REASSIGNED" if replace_existing else "TASK_ASSIGNED"
            Notification.objects.create(
                user=target_user,
                notification_type=notif_type,
                title=f"{'Task Reassigned' if replace_existing else 'New Task Assigned'}: {task.title}",
                message=f"You have been assigned to task '{task.title}' (Priority: {task.priority}, Due: {task.due_date or 'TBD'}) in project '{task.project.code}'.",
                related_project=task.project,
                related_task=task,
            )

    return assignment


@transaction.atomic
def bulk_assign_tasks_service(
    *,
    task_ids: list,
    actor: User | None = None,
    user_id=None,
    assignee_user_ids: list | None = None,
    team_id=None,
    assignee_team_ids: list | None = None,
    task_owner_id=None,
    reviewer_id=None,
    priority: str | None = None,
    due_date=None,
    start_date=None,
    replace_existing: bool = False,
) -> list[Task]:
    """
    SRS §7.2 Bulk Assignment, §7.4 Priority & §7.10 Due Dates:
    Atomically assigns users, teams, task owner, reviewer, priority, or due dates across
    multiple tasks while preserving assignment history and validating company boundaries.
    """
    cleaned_ids = [str(tid) for tid in (task_ids or []) if tid]
    if not cleaned_ids:
        raise ValidationError("Provide at least one task_id for bulk assignment.")

    tasks = list(Task.objects.select_related("project", "project__company").filter(id__in=cleaned_ids))
    if len(tasks) != len(set(cleaned_ids)):
        raise ValidationError("One or more selected tasks were not found.")

    valid_priorities = {choice[0] for choice in Task.Priority.choices}
    if priority and priority not in valid_priorities:
        raise ValidationError(f"Invalid priority '{priority}'. Must be one of {sorted(valid_priorities)}.")

    parsed_due = date.fromisoformat(str(due_date)) if due_date else None
    parsed_start = date.fromisoformat(str(start_date)) if start_date else None

    target_uids = list(assignee_user_ids or [])
    if user_id and str(user_id) not in [str(u) for u in target_uids]:
        target_uids.append(user_id)

    target_tids = list(assignee_team_ids or [])
    if team_id and str(team_id) not in [str(t) for t in target_tids]:
        target_tids.append(team_id)

    updated_tasks = []
    for task in tasks:
        company = task.project.company
        assigner_id = getattr(actor, "id", None) or task.created_by_id

        # 1. User / Team assignments
        if replace_existing and (target_uids or target_tids):
            for active in task.assignments.filter(status__in=["Active", "Acknowledged"]):
                active.end_assignment(status=TaskAssignment.Status.REASSIGNED)

        for uid in target_uids:
            assign_task_service(
                task=task,
                assignment_type=TaskAssignment.AssignmentType.USER,
                user_id=uid,
                assigned_by_id=assigner_id,
                replace_existing=False,
            )

        for tid in target_tids:
            assign_task_service(
                task=task,
                assignment_type=TaskAssignment.AssignmentType.TEAM,
                team_id=tid,
                assigned_by_id=assigner_id,
                replace_existing=False,
            )

        # 2. Direct field updates (task_owner, reviewer, priority, start_date, due_date)
        update_fields = []
        if task_owner_id:
            owner_obj = User.objects.filter(id=task_owner_id).first()
            if not owner_obj:
                raise ValidationError("Selected task owner was not found.")
            _validate_user_in_project_company(owner_obj, company, label="Task owner")
            task.task_owner = owner_obj
            update_fields.append("task_owner")

        if reviewer_id:
            rev_obj = User.objects.filter(id=reviewer_id).first()
            if not rev_obj:
                raise ValidationError("Selected reviewer was not found.")
            _validate_user_in_project_company(rev_obj, company, label="Reviewer")
            task.reviewer = rev_obj
            update_fields.append("reviewer")

        if priority:
            task.priority = priority
            update_fields.append("priority")

        if parsed_start is not None:
            task.start_date = parsed_start
            update_fields.append("start_date")

        if parsed_due is not None:
            eff_start = parsed_start or task.start_date
            if eff_start and parsed_due < eff_start:
                raise ValidationError(
                    f"Task '{task.title}': Due date ({parsed_due}) cannot be earlier than start date ({eff_start})."
                )
            old_due = task.due_date
            task.due_date = parsed_due
            update_fields.append("due_date")
            if old_due != parsed_due:
                TaskComment.objects.create(
                    task=task,
                    author_id=assigner_id,
                    content=f"[Schedule Update] Due date updated from {old_due or 'None'} to {parsed_due}.",
                )

        if update_fields:
            update_fields.append("updated_at")
            task.save(update_fields=list(set(update_fields)))

        task.refresh_from_db()
        updated_tasks.append(task)

    return updated_tasks


@transaction.atomic
def reschedule_task_service(
    *,
    task: Task,
    start_date=None,
    due_date=None,
    estimated_effort=None,
    priority: str | None = None,
    reason: str | None = None,
    actor: User | None = None,
) -> Task:
    """
    SRS §7.4, §7.10 & §22 Rules #10, #12:
    Updates task schedule (start_date, due_date, estimated_effort) and/or priority,
    validates date ordering, records a traceable deadline change comment, and notifies assignees.
    """
    parsed_start = (
        date.fromisoformat(str(start_date))
        if start_date not in (None, "")
        else (None if start_date == "" else task.start_date)
    )
    parsed_due = (
        date.fromisoformat(str(due_date))
        if due_date not in (None, "")
        else (None if due_date == "" else task.due_date)
    )

    if parsed_start and parsed_due and parsed_due < parsed_start:
        raise ValidationError("Due date cannot be earlier than start date (DB Design §5).")

    old_start = task.start_date
    old_due = task.due_date
    old_priority = task.priority

    update_fields = []
    if start_date is not None and parsed_start != old_start:
        task.start_date = parsed_start
        update_fields.append("start_date")

    if due_date is not None and parsed_due != old_due:
        task.due_date = parsed_due
        update_fields.append("due_date")

    if estimated_effort is not None and estimated_effort != "":
        if float(estimated_effort) < 0:
            raise ValidationError("Estimated effort cannot be negative.")
        task.estimated_effort = estimated_effort
        update_fields.append("estimated_effort")

    if priority:
        valid_priorities = {choice[0] for choice in Task.Priority.choices}
        if priority not in valid_priorities:
            raise ValidationError(f"Invalid priority '{priority}'.")
        if priority != old_priority:
            task.priority = priority
            update_fields.append("priority")

    if update_fields:
        update_fields.append("updated_at")
        task.save(update_fields=update_fields)

        author_id = getattr(actor, "id", None) or task.created_by_id
        changes = []
        if "due_date" in update_fields:
            changes.append(f"Due date: {old_due or 'TBD'} → {parsed_due or 'TBD'}")
        if "start_date" in update_fields:
            changes.append(f"Start date: {old_start or 'TBD'} → {parsed_start or 'TBD'}")
        if "priority" in update_fields:
            changes.append(f"Priority: {old_priority} → {task.priority}")

        if changes:
            reason_suffix = f" | Reason: {reason.strip()}" if reason and reason.strip() else ""
            TaskComment.objects.create(
                task=task,
                author_id=author_id,
                content=f"[Schedule & Priority Update] {', '.join(changes)}{reason_suffix}",
            )

        # Notify task owner & active user assignees on deadline or priority change (SRS §13.1)
        if "due_date" in update_fields or "priority" in update_fields:
            recipient_ids = set()
            if task.task_owner_id:
                recipient_ids.add(task.task_owner_id)
            for uid in task.assignments.filter(
                assignment_type=TaskAssignment.AssignmentType.USER,
                status__in=["Active", "Acknowledged"],
                user_id__isnull=False,
            ).values_list("user_id", flat=True):
                recipient_ids.add(uid)

            for uid in recipient_ids:
                Notification.objects.create(
                    user_id=uid,
                    notification_type="DEADLINE_CHANGED",
                    title=f"Schedule / Priority Updated: {task.title}",
                    message=f"Task '{task.title}' updated ({', '.join(changes)}).",
                    related_project=task.project,
                    related_task=task,
                )

    return task


@transaction.atomic
def sync_task_associations_service(
    *,
    task: Task,
    actor_id=None,
    assignee_user_ids: list | None = None,
    assignee_team_ids: list | None = None,
    tag_names: list[str] | None = None,
    dependency_task_ids: list | None = None,
    initial_checklist_items: list[str] | None = None,
) -> Task:
    """
    SRS §7.1 & §7.2:
    Atomically syncs task assignees (users & teams while preserving reassignment history),
    tags, prerequisite task dependencies, and initial checklist steps during task create/update.
    """
    assigner_id = actor_id or task.created_by_id

    # Ensure task_owner is enrolled in ProjectMember
    if task.task_owner_id:
        ProjectMember.objects.get_or_create(
            project=task.project,
            user_id=task.task_owner_id,
            defaults={
                "role_in_project": "Team Member",
                "status": ProjectMember.Status.ACTIVE,
            },
        )

    # 1. Sync User Assignees (preserving history via end_assignment for unselected active assignees)
    if assignee_user_ids is not None:
        target_uids = {str(uid) for uid in assignee_user_ids if uid}
        active_user_assigns = list(
            task.assignments.filter(
                assignment_type=TaskAssignment.AssignmentType.USER,
                status__in=["Active", "Acknowledged"],
            )
        )
        current_uids = {str(a.user_id) for a in active_user_assigns if a.user_id}
        for a in active_user_assigns:
            if str(a.user_id) not in target_uids:
                a.end_assignment(status=TaskAssignment.Status.REASSIGNED)

        for uid_str in target_uids - current_uids:
            assign_task_service(
                task=task,
                assignment_type=TaskAssignment.AssignmentType.USER,
                user_id=uid_str,
                assigned_by_id=assigner_id,
                replace_existing=False,
            )

    # 2. Sync Team Assignees
    if assignee_team_ids is not None:
        target_tids = {str(tid) for tid in assignee_team_ids if tid}
        active_team_assigns = list(
            task.assignments.filter(
                assignment_type=TaskAssignment.AssignmentType.TEAM,
                status__in=["Active", "Acknowledged"],
            )
        )
        current_tids = {str(a.team_id) for a in active_team_assigns if a.team_id}
        for a in active_team_assigns:
            if str(a.team_id) not in target_tids:
                a.end_assignment(status=TaskAssignment.Status.REASSIGNED)

        for tid_str in target_tids - current_tids:
            assign_task_service(
                task=task,
                assignment_type=TaskAssignment.AssignmentType.TEAM,
                team_id=tid_str,
                assigned_by_id=assigner_id,
                replace_existing=False,
            )

    # 3. Sync Task Tags
    if tag_names is not None:
        cleaned_names = []
        seen_lower = set()
        for raw in tag_names:
            name = (raw or "").strip()
            if name and name.lower() not in seen_lower:
                seen_lower.add(name.lower())
                cleaned_names.append(name)

        tag_objs = []
        for name in cleaned_names:
            tag_obj = TaskTag.objects.filter(name__iexact=name).first()
            if not tag_obj:
                tag_obj = TaskTag.objects.create(name=name)
            tag_objs.append(tag_obj)

        target_tag_ids = {t.id for t in tag_objs}
        TaskTagAssignment.objects.filter(task=task).exclude(tag_id__in=target_tag_ids).delete()
        existing_tag_ids = set(
            TaskTagAssignment.objects.filter(task=task).values_list("tag_id", flat=True)
        )
        for t_obj in tag_objs:
            if t_obj.id not in existing_tag_ids:
                TaskTagAssignment.objects.create(task=task, tag=t_obj)

    # 4. Sync Prerequisite Task Dependencies
    if dependency_task_ids is not None:
        target_dep_ids = {str(did) for did in dependency_task_ids if did and str(did) != str(task.id)}
        TaskDependency.objects.filter(task=task).exclude(
            depends_on_task_id__in=target_dep_ids
        ).delete()
        existing_dep_ids = {
            str(did)
            for did in TaskDependency.objects.filter(task=task).values_list(
                "depends_on_task_id", flat=True
            )
        }
        for dep_id_str in target_dep_ids - existing_dep_ids:
            TaskDependency.objects.create(
                task=task,
                depends_on_task_id=dep_id_str,
                dependency_type=TaskDependency.DependencyType.FINISH_TO_START,
            )

    # 5. Initial Checklist Items (on task creation)
    if initial_checklist_items:
        valid_items = [str(item).strip() for item in initial_checklist_items if str(item).strip()]
        if valid_items:
            checklist = Checklist.objects.create(
                task=task,
                title="Task Completion Checklist",
                sort_order=1,
                created_by_id=assigner_id,
            )
            for idx, item_title in enumerate(valid_items, start=1):
                ChecklistItem.objects.create(
                    checklist=checklist,
                    title=item_title,
                    sort_order=idx,
                )

    return task


@transaction.atomic
def delete_task_service(*, task: Task) -> None:
    """
    Deletes a task (and any child subtasks / protected tracking records) cleanly inside
    an atomic transaction, then recalculates parent task and project progress.
    """
    from apps.reviews.models import TaskReview
    from apps.risks.models import Blocker
    from apps.tracking.models import ProgressUpdate, TimeEntry

    project = task.project
    parent_task = task.parent_task

    # Collect task + all descendant subtask IDs
    to_delete_ids = [task.id]
    queue = [task.id]
    while queue:
        curr_id = queue.pop(0)
        child_ids = list(Task.objects.filter(parent_task_id=curr_id).values_list("id", flat=True))
        to_delete_ids.extend(child_ids)
        queue.extend(child_ids)

    ProgressUpdate.objects.filter(task_id__in=to_delete_ids).delete()
    TimeEntry.objects.filter(task_id__in=to_delete_ids).delete()
    TaskReview.objects.filter(task_id__in=to_delete_ids).delete()
    Blocker.objects.filter(task_id__in=to_delete_ids).delete()

    # Delete child subtasks first, then the task itself
    Task.objects.filter(id__in=to_delete_ids).exclude(id=task.id).delete()
    task.delete()

    if parent_task and Task.objects.filter(id=parent_task.id).exists():
        parent_task.refresh_from_db()
        parent_task.recalculate_auto_progress()

    from apps.projects.services import recalculate_project_progress_service

    recalculate_project_progress_service(project=project)


DEFAULT_TASK_TRANSITIONS: dict[str, list[str]] = {
    Task.Status.BACKLOG: [
        Task.Status.TO_DO,
        Task.Status.ASSIGNED,
        Task.Status.IN_PROGRESS,
        Task.Status.CANCELLED,
    ],
    Task.Status.TO_DO: [
        Task.Status.BACKLOG,
        Task.Status.ASSIGNED,
        Task.Status.IN_PROGRESS,
        Task.Status.ON_HOLD,
        Task.Status.BLOCKED,
        Task.Status.CANCELLED,
    ],
    Task.Status.ASSIGNED: [
        Task.Status.TO_DO,
        Task.Status.IN_PROGRESS,
        Task.Status.ON_HOLD,
        Task.Status.BLOCKED,
        Task.Status.CANCELLED,
    ],
    Task.Status.IN_PROGRESS: [
        Task.Status.ASSIGNED,
        Task.Status.ON_HOLD,
        Task.Status.BLOCKED,
        Task.Status.IN_REVIEW,
        Task.Status.COMPLETED,
        Task.Status.CANCELLED,
    ],
    Task.Status.ON_HOLD: [
        Task.Status.TO_DO,
        Task.Status.ASSIGNED,
        Task.Status.IN_PROGRESS,
        Task.Status.BLOCKED,
        Task.Status.CANCELLED,
    ],
    Task.Status.BLOCKED: [
        Task.Status.TO_DO,
        Task.Status.ASSIGNED,
        Task.Status.IN_PROGRESS,
        Task.Status.ON_HOLD,
        Task.Status.CANCELLED,
    ],
    Task.Status.IN_REVIEW: [
        Task.Status.CHANGES_REQUESTED,
        Task.Status.APPROVED,
        Task.Status.IN_PROGRESS,
        Task.Status.BLOCKED,
        Task.Status.ON_HOLD,
        Task.Status.CANCELLED,
    ],
    Task.Status.CHANGES_REQUESTED: [
        Task.Status.IN_PROGRESS,
        Task.Status.IN_REVIEW,
        Task.Status.ON_HOLD,
        Task.Status.BLOCKED,
        Task.Status.CANCELLED,
    ],
    Task.Status.APPROVED: [
        Task.Status.COMPLETED,
        Task.Status.IN_REVIEW,
        Task.Status.CHANGES_REQUESTED,
    ],
    Task.Status.COMPLETED: [
        Task.Status.IN_PROGRESS,
        Task.Status.IN_REVIEW,
    ],
    Task.Status.CANCELLED: [
        Task.Status.BACKLOG,
        Task.Status.TO_DO,
    ],
}


def get_allowed_next_statuses(*, task: Task, actor=None) -> list[str]:
    """
    SRS §7.3, §20.2 & DB Design §4.7 (`workflow_statuses`, `workflow_transitions`):
    Resolves permitted next statuses from company-scoped or global WorkflowTransition rows
    if configured, otherwise falls back to SRS §7.3 DEFAULT_TASK_TRANSITIONS.
    """
    from apps.system_settings.models import WorkflowTransition

    company_id = task.project.company_id
    configured_qs = WorkflowTransition.objects.filter(
        entity_type__iexact="Task",
        company_id=company_id,
    ).select_related("from_status", "to_status", "role")

    if not configured_qs.exists():
        configured_qs = WorkflowTransition.objects.filter(
            entity_type__iexact="Task",
            company__isnull=True,
        ).select_related("from_status", "to_status", "role")

    if configured_qs.exists():
        outgoing = [
            tr for tr in configured_qs if tr.from_status.status_code == task.status and tr.to_status.is_active
        ]
        allowed = [tr.to_status.status_code for tr in outgoing]
    else:
        allowed = list(DEFAULT_TASK_TRANSITIONS.get(task.status, []))

    # Enforce SRS §14.3: if approval_required and not yet approved, Completed is not directly reachable from In Progress
    if task.approval_required and Task.Status.COMPLETED in allowed:
        has_approved_review = task.reviews.filter(decision="Approved").exists()
        if not has_approved_review and task.status != Task.Status.APPROVED:
            allowed = [s for s in allowed if s != Task.Status.COMPLETED]

    # Deduplicate preserving order
    seen = set()
    return [s for s in allowed if not (s in seen or seen.add(s))]


@transaction.atomic
def transition_task_status_service(
    *,
    task: Task,
    new_status: str,
    actor=None,
    reason: str | None = None,
    override_subtasks: bool = False,
    override_reason: str | None = None,
) -> Task:
    """
    SRS §7.3, §8.3, §12.2, §14.3 & §22 Core Business Rules #3, #5, #7, #9:
    Atomically transitions a task's workflow status while validating:
    1. Permitted transition path (configured WorkflowTransition or SRS §7.3 defaults)
    2. Responsible owner / active assignee before active execution
    3. Required review/approval before Completed
    4. Mandatory subtask completion before parent completion (unless authorized override with reason)
    5. Traceable status-change comment, notifications, and progress recalculation
    """
    valid_statuses = {choice[0] for choice in Task.Status.choices}
    if new_status not in valid_statuses:
        raise ValidationError(f"Invalid status '{new_status}'.")

    old_status = task.status
    if new_status == old_status:
        return task

    # 1. Validate transition path (SRS §7.3 & §22 #5)
    allowed_next = get_allowed_next_statuses(task=task, actor=actor)
    if new_status not in allowed_next:
        # Also give a specific, helpful message if approval_required prevented direct completion
        if (
            new_status == Task.Status.COMPLETED
            and task.approval_required
            and old_status != Task.Status.APPROVED
            and not task.reviews.filter(decision="Approved").exists()
        ):
            raise ValidationError(
                "Task requires approval before it can be marked Completed. Move it to 'In Review' and obtain approval first (SRS §14.3 & §22 #7)."
            )
        allowed_label = ", ".join(allowed_next) if allowed_next else "None"
        raise ValidationError(
            f"Invalid status transition from '{old_status}' to '{new_status}'. Permitted next statuses: {allowed_label} (SRS §7.3 & §22 #5)."
        )

    # 2. Enforce responsible owner / active assignee before active execution (SRS §22 #3)
    if new_status in (Task.Status.IN_PROGRESS, Task.Status.IN_REVIEW, Task.Status.COMPLETED):
        has_active_assignment = task.assignments.filter(status__in=["Active", "Acknowledged"]).exists()
        if not task.task_owner_id and not has_active_assignment:
            raise ValidationError(
                f"Task must have a responsible Task Owner or active assignee before moving to '{new_status}' (SRS §22 #3)."
            )

    # 3. Enforce approval & subtask completion rules when completing (SRS §8.3, §14.3, §22 #7, #9)
    if new_status == Task.Status.COMPLETED:
        if task.approval_required:
            has_approved_review = task.reviews.filter(decision="Approved").exists()
            if not has_approved_review and old_status != Task.Status.APPROVED:
                raise ValidationError(
                    "Task requires approval before it can be marked Completed (SRS §14.3 & §22 #7)."
                )
        incomplete_subtasks = task.subtasks.exclude(
            status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED]
        ).count()
        if incomplete_subtasks > 0:
            if override_subtasks:
                if not (override_reason and override_reason.strip()):
                    raise ValidationError(
                        "An authorized override reason is required to complete a task with open subtasks (SRS §8.3 & §22 #9)."
                    )
            else:
                raise ValidationError(
                    f"Cannot complete task while {incomplete_subtasks} subtask(s) are still open. Complete subtasks first or provide an authorized override reason (SRS §8.3 & §22 #9)."
                )
        task.completed_at = timezone.now()
        task.progress = 100.00
    elif old_status == Task.Status.COMPLETED and new_status != Task.Status.COMPLETED:
        # Reopening a completed task clears completed_at
        task.completed_at = None

    task.status = new_status
    task.save()

    # 4. Traceable status transition comment (SRS §7.7 & §22 #6)
    author_id = getattr(actor, "id", None) or task.created_by_id
    note_parts = [f"[Status Transition] {old_status} → {new_status}"]
    if reason and reason.strip():
        note_parts.append(f"Reason: {reason.strip()}")
    if override_subtasks and override_reason and override_reason.strip():
        note_parts.append(f"Subtask Override: {override_reason.strip()}")
    TaskComment.objects.create(
        task=task,
        author_id=author_id,
        content=" | ".join(note_parts),
    )

    # 5. Relevant notifications (SRS §13.1 & §22 #17)
    if new_status == Task.Status.IN_REVIEW and task.reviewer_id:
        Notification.objects.create(
            user_id=task.reviewer_id,
            notification_type="REVIEW_REQUESTED",
            title=f"Review Requested: {task.title}",
            message=f"Task '{task.title}' has been submitted for review.",
            related_project=task.project,
            related_task=task,
        )
    elif new_status == Task.Status.CHANGES_REQUESTED and task.task_owner_id:
        Notification.objects.create(
            user_id=task.task_owner_id,
            notification_type="CHANGES_REQUESTED",
            title=f"Changes Requested: {task.title}",
            message=f"Corrections were requested for task '{task.title}'.",
            related_project=task.project,
            related_task=task,
        )
    elif task.task_owner_id and task.task_owner_id != author_id:
        Notification.objects.create(
            user_id=task.task_owner_id,
            notification_type="STATUS_CHANGED",
            title=f"Task Status Updated: {task.title}",
            message=f"Task '{task.title}' moved from {old_status} to {new_status}.",
            related_project=task.project,
            related_task=task,
        )

    from apps.projects.services import recalculate_project_progress_service

    recalculate_project_progress_service(project=task.project)
    return task


from .attachments import (  # noqa: E402,F401
    ALLOWED_MIME_PREFIXES,
    APPROVED_EXTENSIONS,
    BLOCKED_EXECUTABLE_EXTENSIONS,
    check_attachment_access_permission,
    delete_attachment_service,
    get_file_category,
    remove_physical_file_if_unreferenced,
    store_and_create_attachment_service,
    validate_attachment_file,
)
