from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.models import Notification
from apps.risks.models import Blocker
from apps.system_settings.models import SystemSetting
from apps.tasks.models import Attachment, Task, TaskAttachment
from apps.tasks.services import _validate_user_in_project_company
from apps.tracking.models import ProgressAttachment, ProgressUpdate, TimeEntry


@transaction.atomic
def submit_progress_update_service(
    *,
    task: Task,
    author: User,
    progress_percentage,
    work_completed: str | None = None,
    remaining_work: str | None = None,
    remarks: str | None = None,
    blockers: str | None = None,
    time_spent_minutes: int | None = None,
    next_action: str | None = None,
    attachment_ids: list | None = None,
    progress_method: str | None = None,
    mark_task_blocked: bool = False,
    blocker_category: str = "Execution",
    blocker_impact: str = "High",
    blocker_responsible_person_id=None,
) -> ProgressUpdate:
    """
    SRS §7.5, §7.6, §9.1, §9.2, §9.4, §13.1, §22 (#6, #8, #13) & DB Design §4.5, §5, §7:
    - Validates company membership and percentage/time bounds.
    - Creates an append-only ProgressUpdate record retaining author and timestamp.
    - Links optional attachments via ProgressAttachment (`progress_attachments`).
    - Updates current task progress when `progress_method == 'Manual'` without silently
      overwriting manual progress when automatic methods are used, and rolls up progress
      to parent task and project.
    - Automatically logs a corresponding TimeEntry when `time_spent_minutes > 0`.
    - Automatically creates a Blocker record and optionally transitions task to `Blocked`
      when `blockers` text is reported.
    """
    if not author:
        raise ValidationError("Progress update author is required (SRS §1.4 #5 & §22 #6).")

    company = task.project.company
    _validate_user_in_project_company(author, company, label="Progress update author")

    if task.status == Task.Status.CANCELLED:
        raise ValidationError("Cannot submit progress updates for a cancelled task.")

    try:
        pct_val = Decimal(str(progress_percentage))
    except Exception:
        raise ValidationError("Invalid progress_percentage value.")

    if pct_val < Decimal("0.00") or pct_val > Decimal("100.00"):
        raise ValidationError("Progress percentage must be between 0 and 100 (DB Design §5).")

    if time_spent_minutes is not None and int(time_spent_minutes) < 0:
        raise ValidationError("Time spent minutes cannot be negative.")

    if progress_method:
        valid_methods = {c[0] for c in Task.ProgressMethod.choices}
        if progress_method not in valid_methods:
            raise ValidationError(f"Invalid progress_method '{progress_method}'.")
        if task.progress_method != progress_method:
            task.progress_method = progress_method
            task.save(update_fields=["progress_method", "updated_at"])

    instance = ProgressUpdate.objects.create(
        task=task,
        author=author,
        progress_percentage=pct_val,
        work_completed=work_completed,
        remaining_work=remaining_work,
        remarks=remarks,
        blockers=blockers,
        time_spent_minutes=int(time_spent_minutes) if time_spent_minutes is not None else None,
        next_action=next_action,
    )

    # Link attachments to ProgressUpdate (DB Design §4.5 `progress_attachments`)
    if attachment_ids:
        unique_att_ids = {str(aid) for aid in attachment_ids if aid}
        for aid_str in unique_att_ids:
            att = Attachment.objects.filter(id=aid_str).first()
            if att:
                ProgressAttachment.objects.get_or_create(
                    progress_update=instance,
                    attachment=att,
                )
                TaskAttachment.objects.get_or_create(
                    task=task,
                    attachment=att,
                )

    # Update task progress according to configured calculation method (SRS §7.6 & DB Design §5)
    task_update_fields = []
    if task.progress_method == Task.ProgressMethod.MANUAL:
        task.progress = pct_val
        task_update_fields.append("progress")

    # Auto-transition task to In Progress when work starts and task has a responsible owner
    if (
        pct_val > Decimal("0.00")
        and task.status in [Task.Status.BACKLOG, Task.Status.TO_DO, Task.Status.ASSIGNED]
        and task.task_owner_id
    ):
        task.status = Task.Status.IN_PROGRESS
        task_update_fields.append("status")

    if task_update_fields:
        task_update_fields.append("updated_at")
        task.save(update_fields=list(set(task_update_fields)))
    else:
        # Ensure automatic calculation methods stay fresh and roll up to parent/project
        task.recalculate_auto_progress()

    # SRS §9.1 & §9.2: Record manual TimeEntry when time_spent_minutes > 0
    if instance.time_spent_minutes and instance.time_spent_minutes > 0:
        now_ts = timezone.now()
        TimeEntry.objects.create(
            task=task,
            user=author,
            started_at=now_ts - timedelta(minutes=int(instance.time_spent_minutes)),
            ended_at=now_ts,
            duration_minutes=int(instance.time_spent_minutes),
            entry_type=TimeEntry.EntryType.MANUAL,
            approval_status=TimeEntry.ApprovalStatus.PENDING,
        )

    # SRS §7.5, §9.4 & §18.3: Blocker reporting & optional workflow auto-block
    notified_user_ids = set()
    if instance.blockers and instance.blockers.strip():
        blocker_text = instance.blockers.strip()
        resp_user_id = (
            blocker_responsible_person_id
            or task.task_owner_id
            or task.project.project_manager_id
        )
        if resp_user_id:
            resp_user = User.objects.filter(id=resp_user_id).first()
            if resp_user:
                _validate_user_in_project_company(resp_user, company, label="Blocker responsible person")

        Blocker.objects.create(
            task=task,
            description=blocker_text,
            category=blocker_category or "Execution",
            impact=blocker_impact or "High",
            responsible_person_id=resp_user_id,
            reported_by=author,
            resolution_status=Blocker.ResolutionStatus.OPEN,
        )

        auto_block_setting = (
            SystemSetting.objects.filter(company=company, setting_key="auto_block_on_blocker").first()
            or SystemSetting.objects.filter(company__isnull=True, setting_key="auto_block_on_blocker").first()
        )
        setting_enabled = bool(
            auto_block_setting
            and str(auto_block_setting.setting_value).lower() in ("true", "1", "yes")
        )
        if (mark_task_blocked or setting_enabled) and task.status not in [
            Task.Status.COMPLETED,
            Task.Status.CANCELLED,
            Task.Status.BLOCKED,
        ]:
            task.status = Task.Status.BLOCKED
            task.save(update_fields=["status", "updated_at"])

        blocker_targets = {
            str(uid)
            for uid in [resp_user_id, task.project.project_manager_id, task.task_owner_id]
            if uid and str(uid) != str(author.id)
        }
        for target_uid in blocker_targets:
            Notification.objects.create(
                user_id=target_uid,
                notification_type="BLOCKER_REPORTED",
                title=f"Blocker Reported on Task: {task.title}",
                message=f"{author.full_name} reported a blocker ({instance.progress_percentage}%): {blocker_text[:150]}",
                related_project=task.project,
                related_task=task,
            )
            notified_user_ids.add(target_uid)

    # Notify task owner / reviewer / PM of progress update & remarks
    update_targets = {
        str(uid)
        for uid in [task.task_owner_id, task.reviewer_id, task.project.project_manager_id]
        if uid and str(uid) != str(author.id) and str(uid) not in notified_user_ids
    }
    summary_note = instance.remarks or instance.work_completed or f"Progress updated to {instance.progress_percentage}%"
    for target_uid in update_targets:
        Notification.objects.create(
            user_id=target_uid,
            notification_type="PROGRESS_UPDATED",
            title=f"Progress Update ({instance.progress_percentage}%): {task.title}",
            message=f"{author.full_name}: {str(summary_note)[:150]}",
            related_project=task.project,
            related_task=task,
        )

    return instance


def get_work_log_review_summary_service(queryset) -> dict:
    """
    SRS §9.3 Work Log Review:
    Managers can review work updates and compare:
    1. Planned work versus completed work.
    2. Estimated versus actual time.
    3. Reported progress versus approved deliverables.
    4. Workload between employees.
    5. Time spent on delayed tasks.
    """
    updates = list(
        queryset.select_related(
            "task",
            "task__project",
            "author",
        )
    )
    today = timezone.now().date()

    task_map: dict[str, Task] = {}
    for u in updates:
        task_map[str(u.task_id)] = u.task

    tasks = list(task_map.values())
    total_tasks = len(tasks)
    completed_tasks = sum(1 for t in tasks if t.status == Task.Status.COMPLETED)
    in_progress_tasks = sum(1 for t in tasks if t.status == Task.Status.IN_PROGRESS)
    avg_reported_pct = (
        round(sum(float(u.progress_percentage) for u in updates) / len(updates), 2)
        if updates
        else 0.0
    )
    avg_task_pct = (
        round(sum(float(t.progress) for t in tasks) / total_tasks, 2)
        if total_tasks > 0
        else 0.0
    )

    # Estimated vs Actual Time
    total_estimated_hours = round(
        sum(float(t.estimated_effort or 0) for t in tasks), 2
    )
    total_logged_minutes = sum(int(u.time_spent_minutes or 0) for u in updates)
    # Also compute actual time from time_entries on those tasks
    task_ids = [t.id for t in tasks]
    time_entry_minutes = sum(
        te.duration_minutes or 0
        for te in TimeEntry.objects.filter(task_id__in=task_ids)
    )
    effective_actual_minutes = max(total_logged_minutes, time_entry_minutes)
    total_actual_hours = round(effective_actual_minutes / 60.0, 2)

    # Time spent on delayed/overdue tasks
    delayed_task_ids = {
        str(t.id)
        for t in tasks
        if t.due_date
        and t.due_date < today
        and t.status not in [Task.Status.COMPLETED, Task.Status.CANCELLED]
    }
    delayed_minutes = sum(
        int(u.time_spent_minutes or 0)
        for u in updates
        if str(u.task_id) in delayed_task_ids
    )

    # Reported progress vs Approved deliverables
    review_tasks = [t for t in tasks if t.approval_required]
    approved_or_completed_review_tasks = [
        t for t in review_tasks if t.status in [Task.Status.APPROVED, Task.Status.COMPLETED]
    ]

    # Employee workload comparison
    emp_stats: dict[str, dict] = {}
    for u in updates:
        aid = str(u.author_id)
        if aid not in emp_stats:
            emp_stats[aid] = {
                "author_id": aid,
                "author_name": u.author.full_name,
                "updates_count": 0,
                "task_ids": set(),
                "total_minutes": 0,
                "progress_values": [],
                "blockers_count": 0,
            }
        entry = emp_stats[aid]
        entry["updates_count"] += 1
        entry["task_ids"].add(str(u.task_id))
        entry["total_minutes"] += int(u.time_spent_minutes or 0)
        entry["progress_values"].append(float(u.progress_percentage))
        if u.blockers and u.blockers.strip():
            entry["blockers_count"] += 1

    employee_workload = []
    for entry in emp_stats.values():
        p_vals = entry["progress_values"]
        employee_workload.append(
            {
                "author_id": entry["author_id"],
                "author_name": entry["author_name"],
                "updates_count": entry["updates_count"],
                "tasks_count": len(entry["task_ids"]),
                "total_minutes": entry["total_minutes"],
                "total_hours": round(entry["total_minutes"] / 60.0, 2),
                "average_reported_progress": round(sum(p_vals) / len(p_vals), 2) if p_vals else 0.0,
                "blockers_count": entry["blockers_count"],
            }
        )
    employee_workload.sort(key=lambda x: (-x["total_minutes"], -x["updates_count"]))

    blockers_reported_count = sum(
        1 for u in updates if u.blockers and u.blockers.strip()
    )

    return {
        "total_updates": len(updates),
        "tasks_updated_count": total_tasks,
        "blockers_reported_count": blockers_reported_count,
        "planned_vs_completed": {
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "in_progress_tasks": in_progress_tasks,
            "average_reported_progress": avg_reported_pct,
            "average_task_progress": avg_task_pct,
        },
        "effort_comparison": {
            "total_estimated_hours": total_estimated_hours,
            "total_actual_hours": total_actual_hours,
            "total_logged_minutes": total_logged_minutes,
            "variance_hours": round(total_actual_hours - total_estimated_hours, 2),
        },
        "deliverables_comparison": {
            "approval_required_tasks": len(review_tasks),
            "approved_or_completed_tasks": len(approved_or_completed_review_tasks),
            "pending_approval_tasks": len(review_tasks) - len(approved_or_completed_review_tasks),
        },
        "delayed_tasks_metrics": {
            "delayed_tasks_count": len(delayed_task_ids),
            "time_spent_on_delayed_minutes": delayed_minutes,
            "time_spent_on_delayed_hours": round(delayed_minutes / 60.0, 2),
        },
        "employee_workload": employee_workload,
    }
