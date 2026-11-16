from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from apps.notifications.models import Notification
from apps.projects.models import ProjectMember
from apps.tasks.models import (
    Checklist,
    ChecklistItem,
    Task,
    TaskAssignment,
    TaskDependency,
    TaskTag,
    TaskTagAssignment,
)


@transaction.atomic
def assign_task_service(
    *,
    task: Task,
    assignment_type: str,
    user_id=None,
    team_id=None,
    assigned_by_id=None,
    replace_existing: bool = False,
) -> TaskAssignment:
    """
    Atomically assigns a task to a User or Team while preserving assignment history
    (DB Design §4.4, §5, and §7 Rule #7).
    """
    if replace_existing:
        for active in task.assignments.filter(status__in=["Active", "Acknowledged"]):
            active.end_assignment(status=TaskAssignment.Status.REASSIGNED)

    target_user_id = user_id if assignment_type == TaskAssignment.AssignmentType.USER else None
    target_team_id = team_id if assignment_type == TaskAssignment.AssignmentType.TEAM else None

    # Check if an active assignment for the same target already exists (avoid duplicate active constraint violation)
    if target_user_id:
        existing_active = task.assignments.filter(
            assignment_type=TaskAssignment.AssignmentType.USER,
            user_id=target_user_id,
            status__in=["Active", "Acknowledged"],
        ).first()
        if existing_active:
            return existing_active
    elif target_team_id:
        existing_active = task.assignments.filter(
            assignment_type=TaskAssignment.AssignmentType.TEAM,
            team_id=target_team_id,
            status__in=["Active", "Acknowledged"],
        ).first()
        if existing_active:
            return existing_active

    assignment = TaskAssignment(
        task=task,
        assignment_type=assignment_type,
        user_id=target_user_id,
        team_id=target_team_id,
        assigned_by_id=assigned_by_id or task.created_by_id,
        status=TaskAssignment.Status.ACTIVE,
    )
    assignment.full_clean()
    assignment.save()

    # Ensure assigned user is an active ProjectMember participant on the task's project
    if target_user_id:
        ProjectMember.objects.update_or_create(
            project=task.project,
            user_id=target_user_id,
            defaults={"status": ProjectMember.Status.ACTIVE, "left_at": None},
        )
        Notification.objects.create(
            user_id=target_user_id,
            notification_type="TASK_ASSIGNED",
            title=f"New Task Assigned: {task.title}",
            message=f"You have been assigned to task '{task.title}' in project '{task.project.code}'.",
            related_project=task.project,
            related_task=task,
        )

    if task.status in [Task.Status.BACKLOG, Task.Status.TO_DO]:
        task.status = Task.Status.ASSIGNED
        task.save(update_fields=["status", "updated_at"])

    return assignment


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


@transaction.atomic
def transition_task_status_service(*, task: Task, new_status: str, actor=None) -> Task:
    """
    Atomically transitions a task's workflow status while enforcing review/approval
    and blocker rules (DB Design §4.6 & §7 Rule #7).
    """
    valid_statuses = {choice[0] for choice in Task.Status.choices}
    if new_status not in valid_statuses:
        raise ValidationError(f"Invalid status '{new_status}'.")

    if new_status == Task.Status.COMPLETED:
        if task.approval_required:
            has_approved_review = task.reviews.filter(decision="Approved").exists()
            if not has_approved_review and task.status != Task.Status.APPROVED:
                raise ValidationError(
                    "Task requires approval before it can be marked Completed (DB Design §4.6)."
                )
        incomplete_subtasks = task.subtasks.exclude(
            status__in=[Task.Status.COMPLETED, Task.Status.CANCELLED]
        ).count()
        if incomplete_subtasks > 0:
            raise ValidationError(
                f"Cannot complete task while {incomplete_subtasks} subtasks are still open."
            )
        task.completed_at = timezone.now()
        task.progress = 100.00

    task.status = new_status
    task.save()

    from apps.projects.services import recalculate_project_progress_service

    recalculate_project_progress_service(project=task.project)
    return task
