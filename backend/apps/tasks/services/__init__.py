from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from apps.tasks.models import Task, TaskAssignment


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

    assignment = TaskAssignment(
        task=task,
        assignment_type=assignment_type,
        user_id=user_id if assignment_type == TaskAssignment.AssignmentType.USER else None,
        team_id=team_id if assignment_type == TaskAssignment.AssignmentType.TEAM else None,
        assigned_by_id=assigned_by_id or task.created_by_id,
        status=TaskAssignment.Status.ACTIVE,
    )
    assignment.full_clean()
    assignment.save()

    if task.status in [Task.Status.BACKLOG, Task.Status.TO_DO]:
        task.status = Task.Status.ASSIGNED
        task.save(update_fields=["status", "updated_at"])

    return assignment


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
    task.project.recalculate_progress()
    return task

