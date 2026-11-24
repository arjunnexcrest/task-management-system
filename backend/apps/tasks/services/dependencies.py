from collections import defaultdict, deque
from datetime import date, timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.companies.models import Company
from apps.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
)
from apps.system_settings.models import SystemSetting
from apps.tasks.models import Task, TaskAssignment, TaskDependency


TERMINAL_STATUSES = {Task.Status.COMPLETED, Task.Status.CANCELLED}
STARTED_STATUSES = {
    Task.Status.IN_PROGRESS,
    Task.Status.IN_REVIEW,
    Task.Status.CHANGES_REQUESTED,
    Task.Status.APPROVED,
    Task.Status.COMPLETED,
}
ACTIVE_START_TARGETS = {
    Task.Status.IN_PROGRESS,
    Task.Status.IN_REVIEW,
    Task.Status.APPROVED,
    Task.Status.COMPLETED,
}
AT_RISK_STATUSES = {Task.Status.BLOCKED, Task.Status.ON_HOLD}

DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4]  # Monday..Friday
DEFAULT_HOURS_PER_DAY = 8
DEFAULT_DUE_SOON_DAYS = 3
DEFAULT_DEADLINE_REMINDER_DAYS = 2
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_iso_date(raw) -> date | None:
    if isinstance(raw, date):
        return raw
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except (TypeError, ValueError):
        return None


def _read_int_setting(company: Company | None, key: str, default: int) -> int:
    setting = None
    if company is not None:
        setting = SystemSetting.objects.filter(company=company, setting_key=key).first()
    if setting is None:
        setting = SystemSetting.objects.filter(company__isnull=True, setting_key=key).first()
    if setting is not None:
        val = setting.setting_value
        if isinstance(val, dict) and "days" in val:
            val = val["days"]
        try:
            parsed = int(val)
            if parsed >= 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return default


def get_company_working_calendar(company: Company | None) -> dict:
    """
    SRS §20.1, §20.2 & DB Design §4.1 (`companies.working_calendar`), §4.7 (`system_settings`):
    Resolves effective working days (0=Mon..6=Sun), holiday dates, daily hours,
    `due_soon_days`, and `deadline_reminder_days` without requiring any extra DB tables.
    """
    raw = (getattr(company, "working_calendar", None) or {}) if company else {}
    if not isinstance(raw, dict):
        raw = {}

    raw_days = raw.get("working_days", DEFAULT_WORKING_DAYS)
    working_days_list = sorted(
        {int(d) for d in raw_days if isinstance(d, int) and 0 <= d <= 6}
    )
    if not working_days_list:
        working_days_list = list(DEFAULT_WORKING_DAYS)

    raw_holidays = raw.get("holidays", []) or []
    holidays_set: set[date] = set()
    holidays_iso: list[str] = []
    for h in raw_holidays:
        parsed = _parse_iso_date(h)
        if parsed and parsed not in holidays_set:
            holidays_set.add(parsed)
            holidays_iso.append(parsed.isoformat())
    holidays_iso.sort()

    try:
        hours_per_day = float(raw.get("hours_per_day", DEFAULT_HOURS_PER_DAY))
        if hours_per_day <= 0:
            hours_per_day = float(DEFAULT_HOURS_PER_DAY)
    except (TypeError, ValueError):
        hours_per_day = float(DEFAULT_HOURS_PER_DAY)

    default_due_soon = raw.get("due_soon_days", DEFAULT_DUE_SOON_DAYS)
    try:
        default_due_soon = max(0, int(default_due_soon))
    except (TypeError, ValueError):
        default_due_soon = DEFAULT_DUE_SOON_DAYS

    default_reminder = raw.get("deadline_reminder_days", DEFAULT_DEADLINE_REMINDER_DAYS)
    try:
        default_reminder = max(0, int(default_reminder))
    except (TypeError, ValueError):
        default_reminder = DEFAULT_DEADLINE_REMINDER_DAYS

    due_soon_days = _read_int_setting(company, "due_soon_days", default_due_soon)
    deadline_reminder_days = _read_int_setting(
        company, "deadline_reminder_days", default_reminder
    )

    return {
        "company_id": str(company.id) if company else None,
        "company_name": company.name if company else None,
        "company_code": company.code if company else None,
        "working_days": working_days_list,
        "working_days_set": set(working_days_list),
        "working_day_labels": [WEEKDAY_LABELS[d] for d in working_days_list],
        "holidays": holidays_iso,
        "holidays_set": holidays_set,
        "hours_per_day": hours_per_day,
        "due_soon_days": due_soon_days,
        "deadline_reminder_days": deadline_reminder_days,
    }


def serialize_working_calendar(cal: dict) -> dict:
    """JSON-safe projection of a resolved company working calendar."""
    return {
        "company_id": cal["company_id"],
        "company_name": cal["company_name"],
        "company_code": cal["company_code"],
        "working_days": cal["working_days"],
        "working_day_labels": cal["working_day_labels"],
        "holidays": cal["holidays"],
        "hours_per_day": cal["hours_per_day"],
        "due_soon_days": cal["due_soon_days"],
        "deadline_reminder_days": cal["deadline_reminder_days"],
    }


@transaction.atomic
def update_company_working_calendar_service(
    *,
    company: Company,
    working_days: list[int] | None = None,
    holidays: list[str] | None = None,
    hours_per_day: float | None = None,
    due_soon_days: int | None = None,
    deadline_reminder_days: int | None = None,
    actor: User | None = None,
) -> dict:
    """
    SRS §20.1 & §20.2:
    Persists company working days, holidays, hours/day, and deadline reminder thresholds
    into `companies.working_calendar` and `system_settings`.
    """
    existing = get_company_working_calendar(company)

    next_days = existing["working_days"]
    if working_days is not None:
        if not isinstance(working_days, list) or not working_days:
            raise ValidationError("Provide at least one working day (0=Mon .. 6=Sun).")
        cleaned_days = sorted({int(d) for d in working_days if isinstance(d, int) and 0 <= d <= 6})
        if not cleaned_days:
            raise ValidationError("Working days must contain integers from 0 (Mon) to 6 (Sun).")
        next_days = cleaned_days

    next_holidays = existing["holidays"]
    if holidays is not None:
        if not isinstance(holidays, list):
            raise ValidationError("Holidays must be a list of ISO dates (YYYY-MM-DD).")
        parsed_holidays = []
        seen = set()
        for raw_h in holidays:
            if not str(raw_h).strip():
                continue
            dt = _parse_iso_date(raw_h)
            if dt is None:
                raise ValidationError(f"Invalid holiday date '{raw_h}'. Expected YYYY-MM-DD.")
            iso = dt.isoformat()
            if iso not in seen:
                seen.add(iso)
                parsed_holidays.append(iso)
        parsed_holidays.sort()
        next_holidays = parsed_holidays

    next_hours = existing["hours_per_day"]
    if hours_per_day is not None:
        try:
            h_val = float(hours_per_day)
        except (TypeError, ValueError):
            raise ValidationError("hours_per_day must be a positive number.")
        if h_val <= 0 or h_val > 24:
            raise ValidationError("hours_per_day must be between 0.5 and 24.")
        next_hours = h_val

    next_due_soon = existing["due_soon_days"]
    if due_soon_days is not None:
        try:
            ds_val = int(due_soon_days)
        except (TypeError, ValueError):
            raise ValidationError("due_soon_days must be a non-negative integer.")
        if ds_val < 0 or ds_val > 90:
            raise ValidationError("due_soon_days must be between 0 and 90.")
        next_due_soon = ds_val

    next_reminder = existing["deadline_reminder_days"]
    if deadline_reminder_days is not None:
        try:
            dr_val = int(deadline_reminder_days)
        except (TypeError, ValueError):
            raise ValidationError("deadline_reminder_days must be a non-negative integer.")
        if dr_val < 0 or dr_val > 90:
            raise ValidationError("deadline_reminder_days must be between 0 and 90.")
        next_reminder = dr_val

    company.working_calendar = {
        "working_days": next_days,
        "holidays": next_holidays,
        "hours_per_day": next_hours,
        "due_soon_days": next_due_soon,
        "deadline_reminder_days": next_reminder,
    }
    company.save(update_fields=["working_calendar", "updated_at"])

    actor_obj = actor if getattr(actor, "is_authenticated", False) else None
    SystemSetting.objects.update_or_create(
        company=company,
        setting_key="due_soon_days",
        defaults={"setting_value": next_due_soon, "updated_by": actor_obj},
    )
    SystemSetting.objects.update_or_create(
        company=company,
        setting_key="deadline_reminder_days",
        defaults={"setting_value": next_reminder, "updated_by": actor_obj},
    )

    return serialize_working_calendar(get_company_working_calendar(company))


def is_working_day(target: date, working_days_set: set[int], holidays_set: set[date]) -> bool:
    return target.weekday() in working_days_set and target not in holidays_set


def count_working_days_between(
    start: date,
    end: date,
    working_days_set: set[int],
    holidays_set: set[date],
) -> int:
    """
    Signed count of working days from `start` to `end`:
    - If `end == start`: 0
    - If `end > start`: positive count of working days in (start, end]
    - If `end < start`: negative count of working days in (end, start]
    """
    if start == end:
        return 0
    sign = 1 if end > start else -1
    low, high = (start, end) if end > start else (end, start)
    total_days = (high - low).days

    # Fast exact computation for large ranges + holiday subtraction
    full_weeks, remainder = divmod(total_days, 7)
    work_count = full_weeks * len(working_days_set)
    cursor = low + timedelta(days=full_weeks * 7 + 1)
    while cursor <= high:
        if cursor.weekday() in working_days_set:
            work_count += 1
        cursor += timedelta(days=1)

    if holidays_set:
        holiday_hits = sum(
            1
            for h in holidays_set
            if low < h <= high and h.weekday() in working_days_set
        )
        work_count = max(0, work_count - holiday_hits)

    return sign * work_count


def count_inclusive_working_days(
    start: date,
    end: date,
    working_days_set: set[int],
    holidays_set: set[date],
) -> int:
    """Inclusive count of working days in [start, end]."""
    if end < start:
        return 0
    return count_working_days_between(
        start - timedelta(days=1), end, working_days_set, holidays_set
    )


def compute_task_schedule_metrics(
    task: Task,
    *,
    today: date | None = None,
    calendar_cfg: dict | None = None,
) -> dict:
    """
    SRS §7.10, §20.1, §20.2 & §22 Rule #10:
    Computes calendar and working-day overdue/remaining counts, due status,
    planned working duration, and overdue severity for a task.
    """
    ref_today = today or timezone.now().date()
    cal = calendar_cfg or get_company_working_calendar(
        getattr(getattr(task, "project", None), "company", None)
    )
    working_days_set = cal["working_days_set"]
    holidays_set = cal["holidays_set"]
    due_soon_days = cal["due_soon_days"]

    planned_working_days = None
    if task.start_date and task.due_date and task.due_date >= task.start_date:
        planned_working_days = count_inclusive_working_days(
            task.start_date, task.due_date, working_days_set, holidays_set
        )

    if task.status in TERMINAL_STATUSES:
        return {
            "is_overdue": False,
            "days_remaining": None,
            "days_overdue": 0,
            "working_days_remaining": None,
            "working_days_overdue": 0,
            "planned_working_days": planned_working_days,
            "due_status": "Completed",
            "overdue_severity": "None",
        }

    if not task.due_date:
        return {
            "is_overdue": False,
            "days_remaining": None,
            "days_overdue": 0,
            "working_days_remaining": None,
            "working_days_overdue": 0,
            "planned_working_days": planned_working_days,
            "due_status": "No Due Date",
            "overdue_severity": "None",
        }

    diff_cal = (task.due_date - ref_today).days
    diff_work = count_working_days_between(
        ref_today, task.due_date, working_days_set, holidays_set
    )

    is_overdue = diff_cal < 0
    days_overdue = abs(diff_cal) if is_overdue else 0
    working_days_overdue = abs(diff_work) if is_overdue else 0

    if is_overdue:
        due_status = "Overdue"
        if task.priority == Task.Priority.CRITICAL or days_overdue > 7:
            overdue_severity = "Critical"
        elif task.priority == Task.Priority.HIGH or days_overdue >= 4:
            overdue_severity = "High"
        else:
            overdue_severity = "Warning"
    elif diff_cal == 0:
        due_status = "Due Today"
        overdue_severity = "None"
    elif diff_cal <= due_soon_days:
        due_status = "Due Soon"
        overdue_severity = "None"
    else:
        due_status = "On Track"
        overdue_severity = "None"

    return {
        "is_overdue": is_overdue,
        "days_remaining": diff_cal,
        "days_overdue": days_overdue,
        "working_days_remaining": diff_work,
        "working_days_overdue": working_days_overdue,
        "planned_working_days": planned_working_days,
        "due_status": due_status,
        "overdue_severity": overdue_severity,
    }


def evaluate_dependency_edge(
    *,
    task: Task,
    prereq: Task,
    dependency_type: str,
    today: date | None = None,
) -> dict:
    """
    Evaluates a single directed dependency edge: `task` depends on `prereq` with `dependency_type`.
    Returns start/complete satisfaction, schedule overlap conflict, and overdue/blocked flags.
    """
    ref_today = today or timezone.now().date()
    prereq_started = prereq.status in STARTED_STATUSES
    prereq_finished = prereq.status == Task.Status.COMPLETED
    prereq_is_overdue = bool(
        prereq.due_date
        and prereq.status not in TERMINAL_STATUSES
        and prereq.due_date < ref_today
    )
    prereq_is_blocked = prereq.status in AT_RISK_STATUSES

    is_satisfied_for_start = True
    is_satisfied_for_complete = True
    start_block_reason = None
    complete_block_reason = None
    schedule_conflict = None
    slip_days = 0

    if dependency_type == TaskDependency.DependencyType.FINISH_TO_START:
        is_satisfied_for_start = prereq_finished
        is_satisfied_for_complete = prereq_finished
        if not prereq_finished:
            msg = (
                f"Prerequisite '{prereq.title}' (Finish-to-Start) must be Completed "
                f"before starting/completing '{task.title}' (currently '{prereq.status}')."
            )
            start_block_reason = msg
            complete_block_reason = msg
        if prereq.due_date and task.start_date and task.start_date < prereq.due_date:
            slip_days = (prereq.due_date - task.start_date).days
            schedule_conflict = (
                f"Schedule overlap (FS): '{task.title}' starts on {task.start_date}, "
                f"which is {slip_days}d before prerequisite '{prereq.title}' is due ({prereq.due_date})."
            )

    elif dependency_type == TaskDependency.DependencyType.START_TO_START:
        is_satisfied_for_start = prereq_started
        is_satisfied_for_complete = prereq_started
        if not prereq_started:
            msg = (
                f"Prerequisite '{prereq.title}' (Start-to-Start) must be started "
                f"before starting '{task.title}' (currently '{prereq.status}')."
            )
            start_block_reason = msg
            complete_block_reason = msg
        if prereq.start_date and task.start_date and task.start_date < prereq.start_date:
            slip_days = (prereq.start_date - task.start_date).days
            schedule_conflict = (
                f"Schedule overlap (SS): '{task.title}' starts on {task.start_date}, "
                f"{slip_days}d before prerequisite '{prereq.title}' starts ({prereq.start_date})."
            )

    elif dependency_type == TaskDependency.DependencyType.FINISH_TO_FINISH:
        is_satisfied_for_start = True
        is_satisfied_for_complete = prereq_finished
        if not prereq_finished:
            complete_block_reason = (
                f"Prerequisite '{prereq.title}' (Finish-to-Finish) must be Completed "
                f"before completing '{task.title}' (currently '{prereq.status}')."
            )
        if prereq.due_date and task.due_date and task.due_date < prereq.due_date:
            slip_days = (prereq.due_date - task.due_date).days
            schedule_conflict = (
                f"Schedule overlap (FF): '{task.title}' is due {task.due_date}, "
                f"{slip_days}d before prerequisite '{prereq.title}' finishes ({prereq.due_date})."
            )

    elif dependency_type == TaskDependency.DependencyType.START_TO_FINISH:
        is_satisfied_for_start = True
        is_satisfied_for_complete = prereq_started
        if not prereq_started:
            complete_block_reason = (
                f"Prerequisite '{prereq.title}' (Start-to-Finish) must be started "
                f"before completing '{task.title}' (currently '{prereq.status}')."
            )
        if prereq.start_date and task.due_date and task.due_date < prereq.start_date:
            slip_days = (prereq.start_date - task.due_date).days
            schedule_conflict = (
                f"Schedule overlap (SF): '{task.title}' is due {task.due_date}, "
                f"{slip_days}d before prerequisite '{prereq.title}' starts ({prereq.start_date})."
            )

    return {
        "is_satisfied_for_start": is_satisfied_for_start,
        "is_satisfied_for_complete": is_satisfied_for_complete,
        "start_block_reason": start_block_reason,
        "complete_block_reason": complete_block_reason,
        "prereq_is_overdue": prereq_is_overdue,
        "prereq_is_blocked": prereq_is_blocked,
        "schedule_conflict": schedule_conflict,
        "slip_days": slip_days,
    }


def validate_task_dependency_service(
    *,
    task: Task,
    depends_on_task: Task,
    dependency_type: str = TaskDependency.DependencyType.FINISH_TO_START,
) -> dict:
    """
    SRS §7.9 & DB Design §4.4, §5:
    Validates no self-dependency, same-project boundary, valid dependency type,
    and transitive DAG acyclicity (rejects A -> B -> ... -> A cycles).
    """
    valid_types = {choice[0] for choice in TaskDependency.DependencyType.choices}
    if dependency_type not in valid_types:
        raise ValidationError(
            f"Invalid dependency_type '{dependency_type}'. Must be one of {sorted(valid_types)}."
        )

    if task.id == depends_on_task.id:
        raise ValidationError("A task cannot depend on itself (DB Design §5).")

    if task.project_id != depends_on_task.project_id:
        raise ValidationError(
            "Dependent tasks must belong to the same project (DB Design §4.4 & §5)."
        )

    # Load all existing project dependency edges once for fast transitive cycle check
    edges = list(
        TaskDependency.objects.filter(task__project_id=task.project_id).values_list(
            "task_id", "depends_on_task_id"
        )
    )
    prereqs_by_task: dict = defaultdict(list)
    for t_id, dep_id in edges:
        prereqs_by_task[t_id].append(dep_id)

    visited = {task.id}
    queue = deque([depends_on_task.id])
    while queue:
        curr_id = queue.popleft()
        if curr_id in visited:
            raise ValidationError(
                "Circular task dependency detected; dependency cycles are not allowed (DB Design §5)."
            )
        visited.add(curr_id)
        for nxt in prereqs_by_task.get(curr_id, ()):
            queue.append(nxt)

    return evaluate_dependency_edge(
        task=task,
        prereq=depends_on_task,
        dependency_type=dependency_type,
    )


def evaluate_task_dependencies(
    task: Task,
    *,
    today: date | None = None,
    prefetched_deps=None,
) -> dict:
    """
    Evaluates all upstream prerequisite dependencies for `task` using prefetched
    `task.dependencies.all()` where available (zero N+1 queries on list views).
    """
    ref_today = today or timezone.now().date()
    deps = list(prefetched_deps) if prefetched_deps is not None else list(task.dependencies.all())

    unsatisfied_for_start = []
    unsatisfied_for_complete = []
    schedule_conflicts = []
    at_risk_prerequisites = []

    for dep in deps:
        prereq = dep.depends_on_task
        ev = evaluate_dependency_edge(
            task=task,
            prereq=prereq,
            dependency_type=dep.dependency_type,
            today=ref_today,
        )
        entry = {
            "dependency_id": str(dep.id),
            "depends_on_task_id": str(prereq.id),
            "depends_on_title": prereq.title,
            "depends_on_status": prereq.status,
            "depends_on_priority": prereq.priority,
            "depends_on_due_date": prereq.due_date.isoformat() if prereq.due_date else None,
            "dependency_type": dep.dependency_type,
            "is_overdue": ev["prereq_is_overdue"],
            "is_blocked": ev["prereq_is_blocked"],
            "reason": ev["start_block_reason"] or ev["complete_block_reason"],
        }
        if not ev["is_satisfied_for_start"]:
            unsatisfied_for_start.append(entry)
        if not ev["is_satisfied_for_complete"]:
            unsatisfied_for_complete.append(entry)
        if ev["schedule_conflict"]:
            schedule_conflicts.append(
                {
                    "dependency_id": str(dep.id),
                    "depends_on_task_id": str(prereq.id),
                    "depends_on_title": prereq.title,
                    "dependency_type": dep.dependency_type,
                    "slip_days": ev["slip_days"],
                    "message": ev["schedule_conflict"],
                }
            )
        if (ev["prereq_is_overdue"] or ev["prereq_is_blocked"]) and (
            not ev["is_satisfied_for_start"] or not ev["is_satisfied_for_complete"]
        ):
            at_risk_prerequisites.append(entry)

    if task.status in TERMINAL_STATUSES:
        blocking_prerequisites = []
    elif task.status in STARTED_STATUSES:
        blocking_prerequisites = unsatisfied_for_complete
    else:
        blocking_prerequisites = unsatisfied_for_start

    return {
        "is_dependency_blocked": len(blocking_prerequisites) > 0,
        "blocking_prerequisites": blocking_prerequisites,
        "blocking_prerequisites_count": len(blocking_prerequisites),
        "unsatisfied_for_start": unsatisfied_for_start,
        "unsatisfied_for_complete": unsatisfied_for_complete,
        "schedule_conflicts": schedule_conflicts,
        "at_risk_prerequisites": at_risk_prerequisites,
        "has_schedule_conflicts": len(schedule_conflicts) > 0,
    }


def check_dependency_rules_for_transition(
    *,
    task: Task,
    new_status: str,
    override_dependencies: bool = False,
    override_reason: str | None = None,
) -> None:
    """
    SRS §7.9 & §22 Rule #20:
    Enforces prerequisite dependency constraints when moving a task into active execution
    (`In Progress`, `In Review`, `Approved`) or `Completed`, unless an authorized override
    with a mandatory `override_reason` is supplied.
    """
    if new_status not in ACTIVE_START_TARGETS:
        return

    deps = list(task.dependencies.select_related("depends_on_task").all())
    if not deps:
        return

    eval_result = evaluate_task_dependencies(task, prefetched_deps=deps)

    if new_status == Task.Status.COMPLETED:
        # Completing requires both start and completion prerequisites satisfied
        seen_ids = set()
        blocking = []
        for item in eval_result["unsatisfied_for_start"] + eval_result["unsatisfied_for_complete"]:
            if item["dependency_id"] not in seen_ids:
                seen_ids.add(item["dependency_id"])
                blocking.append(item)
    else:
        blocking = eval_result["unsatisfied_for_start"]

    if not blocking:
        return

    if override_dependencies:
        if not (override_reason and override_reason.strip()):
            raise ValidationError(
                "An authorized override_reason is required to bypass unsatisfied prerequisite dependencies (SRS §7.9 & §22 #20)."
            )
        return

    details = "; ".join(
        f"'{b['depends_on_title']}' [{b['dependency_type']}, status: {b['depends_on_status']}]"
        for b in blocking[:5]
    )
    extra = f" (+{len(blocking) - 5} more)" if len(blocking) > 5 else ""
    raise ValidationError(
        f"Cannot transition task to '{new_status}' because {len(blocking)} prerequisite "
        f"dependency rule(s) are unsatisfied: {details}{extra}. Complete/start prerequisite tasks "
        f"first or provide override_dependencies with an authorized override_reason (SRS §7.9 & §22 #20)."
    )


def analyze_task_dependency_impact(
    task: Task,
    *,
    today: date | None = None,
    calendar_cfg: dict | None = None,
) -> dict:
    """
    SRS §7.9 & §12.4:
    Performs full upstream & transitive downstream dependency graph analysis for `task`,
    detecting schedule conflicts and downstream delay ripple warnings for managers.
    """
    ref_today = today or timezone.now().date()
    cal = calendar_cfg or get_company_working_calendar(task.project.company)

    # Load all project tasks & dependency edges in 2 queries total
    project_tasks = {
        t.id: t
        for t in Task.objects.filter(project_id=task.project_id).select_related(
            "task_owner", "project", "project__company"
        )
    }
    all_edges = list(
        TaskDependency.objects.filter(task__project_id=task.project_id).select_related(
            "task", "task__task_owner", "depends_on_task", "depends_on_task__task_owner"
        )
    )

    upstream_edges_by_task: dict = defaultdict(list)
    downstream_edges_by_prereq: dict = defaultdict(list)
    for edge in all_edges:
        upstream_edges_by_task[edge.task_id].append(edge)
        downstream_edges_by_prereq[edge.depends_on_task_id].append(edge)

    source_task = project_tasks.get(task.id, task)
    source_metrics = compute_task_schedule_metrics(
        source_task, today=ref_today, calendar_cfg=cal
    )
    upstream_eval = evaluate_task_dependencies(
        source_task,
        today=ref_today,
        prefetched_deps=upstream_edges_by_task.get(source_task.id, []),
    )

    # Traverse downstream dependent tasks via BFS (preserving shortest hop distance)
    direct_dependents = []
    transitive_dependents = []
    delay_warnings = []

    visited_downstream: dict = {}
    queue = deque()
    for edge in downstream_edges_by_prereq.get(source_task.id, []):
        queue.append((edge, 1, source_task))

    while queue:
        edge, hop, via_task = queue.popleft()
        dep_task = project_tasks.get(edge.task_id, edge.task)
        if dep_task.id == source_task.id or dep_task.id in visited_downstream:
            continue
        visited_downstream[dep_task.id] = hop

        dep_metrics = compute_task_schedule_metrics(
            dep_task, today=ref_today, calendar_cfg=cal
        )
        edge_eval = evaluate_dependency_edge(
            task=dep_task,
            prereq=via_task,
            dependency_type=edge.dependency_type,
            today=ref_today,
        )

        is_open_dependent = dep_task.status not in TERMINAL_STATUSES
        dep_item = {
            "dependency_id": str(edge.id),
            "task_id": str(dep_task.id),
            "title": dep_task.title,
            "status": dep_task.status,
            "priority": dep_task.priority,
            "start_date": dep_task.start_date.isoformat() if dep_task.start_date else None,
            "due_date": dep_task.due_date.isoformat() if dep_task.due_date else None,
            "task_owner_id": str(dep_task.task_owner_id) if dep_task.task_owner_id else None,
            "task_owner_name": dep_task.task_owner.full_name if dep_task.task_owner else None,
            "dependency_type": edge.dependency_type,
            "via_task_id": str(via_task.id),
            "via_task_title": via_task.title,
            "hop_distance": hop,
            "is_overdue": dep_metrics["is_overdue"],
            "due_status": dep_metrics["due_status"],
            "schedule_conflict": edge_eval["schedule_conflict"],
            "slip_days": edge_eval["slip_days"],
        }
        transitive_dependents.append(dep_item)
        if hop == 1:
            direct_dependents.append(dep_item)

        # Build manager delay warnings for open downstream tasks
        if is_open_dependent:
            if source_metrics["is_overdue"]:
                severity = (
                    "Critical"
                    if (
                        source_metrics["overdue_severity"] == "Critical"
                        or dep_task.priority == Task.Priority.CRITICAL
                    )
                    else "High"
                )
                delay_warnings.append(
                    {
                        "severity": severity,
                        "reason": "Prerequisite Overdue",
                        "source_task_id": str(source_task.id),
                        "source_task_title": source_task.title,
                        "affected_task_id": str(dep_task.id),
                        "affected_task_title": dep_task.title,
                        "affected_task_status": dep_task.status,
                        "affected_task_due_date": dep_task.due_date.isoformat()
                        if dep_task.due_date
                        else None,
                        "affected_task_owner_name": dep_task.task_owner.full_name
                        if dep_task.task_owner
                        else None,
                        "dependency_type": edge.dependency_type,
                        "hop_distance": hop,
                        "slip_days": source_metrics["days_overdue"],
                        "message": (
                            f"Delay risk (hop {hop}): Prerequisite '{source_task.title}' is "
                            f"{source_metrics['days_overdue']}d ({source_metrics['working_days_overdue']} working d) "
                            f"overdue, threatening dependent task '{dep_task.title}' (due {dep_task.due_date or 'TBD'})."
                        ),
                    }
                )
            elif source_task.status in AT_RISK_STATUSES:
                delay_warnings.append(
                    {
                        "severity": "High",
                        "reason": f"Prerequisite {source_task.status}",
                        "source_task_id": str(source_task.id),
                        "source_task_title": source_task.title,
                        "affected_task_id": str(dep_task.id),
                        "affected_task_title": dep_task.title,
                        "affected_task_status": dep_task.status,
                        "affected_task_due_date": dep_task.due_date.isoformat()
                        if dep_task.due_date
                        else None,
                        "affected_task_owner_name": dep_task.task_owner.full_name
                        if dep_task.task_owner
                        else None,
                        "dependency_type": edge.dependency_type,
                        "hop_distance": hop,
                        "slip_days": 0,
                        "message": (
                            f"Downstream risk (hop {hop}): Prerequisite '{source_task.title}' is "
                            f"currently '{source_task.status}', which may delay '{dep_task.title}'."
                        ),
                    }
                )
            if edge_eval["schedule_conflict"] and hop == 1:
                delay_warnings.append(
                    {
                        "severity": "Warning" if edge_eval["slip_days"] <= 3 else "High",
                        "reason": "Schedule Overlap",
                        "source_task_id": str(source_task.id),
                        "source_task_title": source_task.title,
                        "affected_task_id": str(dep_task.id),
                        "affected_task_title": dep_task.title,
                        "affected_task_status": dep_task.status,
                        "affected_task_due_date": dep_task.due_date.isoformat()
                        if dep_task.due_date
                        else None,
                        "affected_task_owner_name": dep_task.task_owner.full_name
                        if dep_task.task_owner
                        else None,
                        "dependency_type": edge.dependency_type,
                        "hop_distance": hop,
                        "slip_days": edge_eval["slip_days"],
                        "message": edge_eval["schedule_conflict"],
                    }
                )

        for next_edge in downstream_edges_by_prereq.get(dep_task.id, []):
            queue.append((next_edge, hop + 1, dep_task))

    open_downstream_count = sum(
        1 for d in transitive_dependents if d["status"] not in TERMINAL_STATUSES
    )
    is_critical_bottleneck = bool(
        open_downstream_count > 0
        and (
            source_metrics["is_overdue"]
            or source_task.status in AT_RISK_STATUSES
            or len(delay_warnings) > 0
        )
    )

    return {
        "task_id": str(source_task.id),
        "task_title": source_task.title,
        "task_status": source_task.status,
        "schedule_metrics": source_metrics,
        "is_dependency_blocked": upstream_eval["is_dependency_blocked"],
        "blocking_prerequisites": upstream_eval["blocking_prerequisites"],
        "blocking_prerequisites_count": upstream_eval["blocking_prerequisites_count"],
        "unsatisfied_for_start": upstream_eval["unsatisfied_for_start"],
        "unsatisfied_for_complete": upstream_eval["unsatisfied_for_complete"],
        "schedule_conflicts": upstream_eval["schedule_conflicts"],
        "at_risk_prerequisites": upstream_eval["at_risk_prerequisites"],
        "direct_dependents": direct_dependents,
        "transitive_dependents": transitive_dependents,
        "downstream_direct_count": len(direct_dependents),
        "downstream_total_count": len(transitive_dependents),
        "open_downstream_count": open_downstream_count,
        "delay_warnings": delay_warnings,
        "is_critical_bottleneck": is_critical_bottleneck,
    }


def _dispatch_notification_with_delivery(
    *,
    user_id,
    notification_type: str,
    title: str,
    message: str,
    related_project,
    related_task,
    dedupe_hours: int = 24,
) -> Notification | None:
    """
    Creates a `Notification` and `NotificationDelivery` respecting `NotificationPreference`
    and deduplicating recent alerts of the same type for the same user & task.
    """
    if not user_id:
        return None

    pref = NotificationPreference.objects.filter(
        user_id=user_id, notification_type=notification_type
    ).first()
    in_app_enabled = pref.in_app_enabled if pref else True
    email_enabled = pref.email_enabled if pref else False

    if not in_app_enabled and not email_enabled:
        return None

    if dedupe_hours and dedupe_hours > 0:
        cutoff = timezone.now() - timedelta(hours=dedupe_hours)
        already_sent = Notification.objects.filter(
            user_id=user_id,
            notification_type=notification_type,
            related_task=related_task,
            created_at__gte=cutoff,
        ).exists()
        if already_sent:
            return None

    notif = Notification.objects.create(
        user_id=user_id,
        notification_type=notification_type,
        title=title[:200],
        message=message,
        related_project=related_project,
        related_task=related_task,
    )
    now = timezone.now()
    if in_app_enabled:
        NotificationDelivery.objects.create(
            notification=notif,
            channel=NotificationDelivery.Channel.IN_APP,
            delivery_status=NotificationDelivery.DeliveryStatus.SENT,
            sent_at=now,
        )
    if email_enabled:
        NotificationDelivery.objects.create(
            notification=notif,
            channel=NotificationDelivery.Channel.EMAIL,
            delivery_status=NotificationDelivery.DeliveryStatus.PENDING,
        )
    return notif


def notify_dependency_delay_impact(
    *,
    task: Task,
    trigger_reason: str,
    actor: User | None = None,
    dedupe_hours: int = 24,
) -> list[Notification]:
    """
    SRS §7.9 & §13.1:
    Warns the Project Manager and downstream dependent task owners whenever a prerequisite
    delay, reschedule slip, or Blocked/On Hold status threatens dependent work.
    """
    impact = analyze_task_dependency_impact(task)
    open_downstream = [
        d for d in impact["transitive_dependents"] if d["status"] not in TERMINAL_STATUSES
    ]
    if not open_downstream:
        return []

    affected_titles = ", ".join(f"'{d['title']}'" for d in open_downstream[:4])
    if len(open_downstream) > 4:
        affected_titles += f" (+{len(open_downstream) - 4} more)"

    recipient_ids = set()
    if task.project.project_manager_id:
        recipient_ids.add(task.project.project_manager_id)
    for d in open_downstream:
        if d["task_owner_id"]:
            recipient_ids.add(d["task_owner_id"])

    # Also include active assignees of directly impacted open tasks
    direct_open_ids = [
        d["task_id"] for d in impact["direct_dependents"] if d["status"] not in TERMINAL_STATUSES
    ]
    if direct_open_ids:
        for uid in TaskAssignment.objects.filter(
            task_id__in=direct_open_ids,
            assignment_type=TaskAssignment.AssignmentType.USER,
            status__in=["Active", "Acknowledged"],
            user_id__isnull=False,
        ).values_list("user_id", flat=True):
            recipient_ids.add(uid)

    created = []
    for uid in recipient_ids:
        notif = _dispatch_notification_with_delivery(
            user_id=uid,
            notification_type="DEPENDENCY_DELAY_WARNING",
            title=f"Dependency Delay Warning: {task.title}",
            message=(
                f"{trigger_reason} on prerequisite task '{task.title}' "
                f"(Status: {task.status}, Due: {task.due_date or 'TBD'}). "
                f"{len(open_downstream)} dependent task(s) may be impacted: {affected_titles}."
            ),
            related_project=task.project,
            related_task=task,
            dedupe_hours=dedupe_hours,
        )
        if notif:
            created.append(notif)
    return created


def build_project_dependency_graph_service(
    *,
    project_id,
    tasks_queryset=None,
    today: date | None = None,
) -> dict:
    """
    SRS §7.9 & §12.4:
    Builds a project-scoped dependency DAG with topological tiers, working-day overdue
    metrics, schedule conflicts, and downstream delay warnings for Gantt/Timeline rendering.
    """
    ref_today = today or timezone.now().date()
    if tasks_queryset is not None:
        tasks = list(tasks_queryset.filter(project_id=project_id))
    else:
        tasks = list(
            Task.objects.filter(project_id=project_id)
            .select_related("project", "project__company", "task_owner", "phase", "parent_task")
            .prefetch_related("dependencies__depends_on_task", "dependent_tasks__task")
            .order_by("start_date", "due_date", "created_at")
        )

    task_map = {t.id: t for t in tasks}
    edges_qs = TaskDependency.objects.filter(
        task__project_id=project_id
    ).select_related("task", "task__task_owner", "depends_on_task", "depends_on_task__task_owner")
    all_edges = list(edges_qs)

    company = tasks[0].project.company if tasks else None
    cal = get_company_working_calendar(company)

    # Topological level computation (Kahn's algorithm)
    in_degree = {t.id: 0 for t in tasks}
    outgoing: dict = defaultdict(list)
    upstream_by_task: dict = defaultdict(list)
    downstream_by_task: dict = defaultdict(list)

    for edge in all_edges:
        if edge.task_id in task_map and edge.depends_on_task_id in task_map:
            outgoing[edge.depends_on_task_id].append(edge.task_id)
            in_degree[edge.task_id] = in_degree.get(edge.task_id, 0) + 1
            upstream_by_task[edge.task_id].append(edge)
            downstream_by_task[edge.depends_on_task_id].append(edge)

    level = {t.id: 0 for t in tasks}
    q = deque([tid for tid, deg in in_degree.items() if deg == 0])
    while q:
        curr = q.popleft()
        for nxt in outgoing.get(curr, ()):
            level[nxt] = max(level.get(nxt, 0), level[curr] + 1)
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                q.append(nxt)

    nodes = []
    project_warnings = []
    blocked_tasks_count = 0
    overdue_tasks_count = 0
    schedule_conflicts_count = 0
    at_risk_dependents_ids = set()

    for t in tasks:
        metrics = compute_task_schedule_metrics(t, today=ref_today, calendar_cfg=cal)
        up_eval = evaluate_task_dependencies(
            t, today=ref_today, prefetched_deps=upstream_by_task.get(t.id, [])
        )
        if metrics["is_overdue"]:
            overdue_tasks_count += 1
        if up_eval["is_dependency_blocked"]:
            blocked_tasks_count += 1

        down_edges = downstream_by_task.get(t.id, [])
        if metrics["is_overdue"] or t.status in AT_RISK_STATUSES:
            for de in down_edges:
                if de.task.status not in TERMINAL_STATUSES:
                    at_risk_dependents_ids.add(de.task_id)
                    reason = (
                        "Prerequisite Overdue"
                        if metrics["is_overdue"]
                        else f"Prerequisite {t.status}"
                    )
                    project_warnings.append(
                        {
                            "severity": "Critical"
                            if (
                                metrics["overdue_severity"] == "Critical"
                                or de.task.priority == Task.Priority.CRITICAL
                            )
                            else "High",
                            "reason": reason,
                            "source_task_id": str(t.id),
                            "source_task_title": t.title,
                            "affected_task_id": str(de.task_id),
                            "affected_task_title": de.task.title,
                            "affected_task_status": de.task.status,
                            "affected_task_due_date": de.task.due_date.isoformat()
                            if de.task.due_date
                            else None,
                            "dependency_type": de.dependency_type,
                            "slip_days": metrics["days_overdue"],
                            "message": (
                                f"'{t.title}' ({reason}) threatens dependent task '{de.task.title}' "
                                f"[{de.dependency_type}]."
                            ),
                        }
                    )

        nodes.append(
            {
                "id": str(t.id),
                "title": t.title,
                "is_subtask": t.is_subtask,
                "parent_task": str(t.parent_task_id) if t.parent_task_id else None,
                "phase": str(t.phase_id) if t.phase_id else None,
                "phase_name": t.phase.name if t.phase_id and t.phase else None,
                "status": t.status,
                "priority": t.priority,
                "progress": float(t.progress),
                "start_date": t.start_date.isoformat() if t.start_date else None,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "task_owner_id": str(t.task_owner_id) if t.task_owner_id else None,
                "task_owner_name": t.task_owner.full_name if t.task_owner else None,
                "topological_level": level.get(t.id, 0),
                "upstream_count": len(upstream_by_task.get(t.id, [])),
                "downstream_count": len(down_edges),
                "is_dependency_blocked": up_eval["is_dependency_blocked"],
                "blocking_prerequisites_count": up_eval["blocking_prerequisites_count"],
                "has_schedule_conflicts": up_eval["has_schedule_conflicts"],
                **metrics,
            }
        )

    edges_out = []
    for edge in all_edges:
        if edge.task_id not in task_map or edge.depends_on_task_id not in task_map:
            continue
        ev = evaluate_dependency_edge(
            task=edge.task,
            prereq=edge.depends_on_task,
            dependency_type=edge.dependency_type,
            today=ref_today,
        )
        if ev["schedule_conflict"]:
            schedule_conflicts_count += 1
            at_risk_dependents_ids.add(edge.task_id)
            project_warnings.append(
                {
                    "severity": "Warning" if ev["slip_days"] <= 3 else "High",
                    "reason": "Schedule Overlap",
                    "source_task_id": str(edge.depends_on_task_id),
                    "source_task_title": edge.depends_on_task.title,
                    "affected_task_id": str(edge.task_id),
                    "affected_task_title": edge.task.title,
                    "affected_task_status": edge.task.status,
                    "affected_task_due_date": edge.task.due_date.isoformat()
                    if edge.task.due_date
                    else None,
                    "dependency_type": edge.dependency_type,
                    "slip_days": ev["slip_days"],
                    "message": ev["schedule_conflict"],
                }
            )
        edges_out.append(
            {
                "id": str(edge.id),
                "task": str(edge.task_id),
                "task_title": edge.task.title,
                "task_status": edge.task.status,
                "depends_on_task": str(edge.depends_on_task_id),
                "depends_on_title": edge.depends_on_task.title,
                "depends_on_status": edge.depends_on_task.status,
                "dependency_type": edge.dependency_type,
                "is_satisfied_for_start": ev["is_satisfied_for_start"],
                "is_satisfied_for_complete": ev["is_satisfied_for_complete"],
                "has_schedule_conflict": bool(ev["schedule_conflict"]),
                "schedule_conflict": ev["schedule_conflict"],
                "slip_days": ev["slip_days"],
            }
        )

    return {
        "project_id": str(project_id),
        "as_of_date": ref_today.isoformat(),
        "working_calendar": serialize_working_calendar(cal),
        "summary": {
            "total_tasks": len(nodes),
            "total_dependencies": len(edges_out),
            "blocked_tasks_count": blocked_tasks_count,
            "overdue_tasks_count": overdue_tasks_count,
            "schedule_conflicts_count": schedule_conflicts_count,
            "at_risk_dependents_count": len(at_risk_dependents_ids),
            "warnings_count": len(project_warnings),
        },
        "nodes": nodes,
        "edges": edges_out,
        "warnings": project_warnings,
    }


def compute_overdue_summary_service(
    *,
    tasks_queryset,
    today: date | None = None,
) -> dict:
    """
    SRS §7.10, §10.5 & §27.3:
    Computes overdue, due-today, due-soon, and dependency-blocked KPIs and categorized task
    lists across the authorized queryset, respecting each company's working calendar.
    """
    ref_today = today or timezone.now().date()
    tasks = list(
        tasks_queryset.select_related(
            "project", "project__company", "task_owner", "department", "team"
        ).prefetch_related("dependencies__depends_on_task", "dependent_tasks__task")
    )

    cal_cache: dict = {}
    overdue_items = []
    due_today_items = []
    due_soon_items = []
    dependency_blocked_items = []
    critical_overdue_count = 0
    high_overdue_count = 0
    warning_overdue_count = 0
    at_risk_dependent_ids = set()

    for t in tasks:
        comp = t.project.company
        if comp.id not in cal_cache:
            cal_cache[comp.id] = get_company_working_calendar(comp)
        cal = cal_cache[comp.id]

        metrics = compute_task_schedule_metrics(t, today=ref_today, calendar_cfg=cal)
        dep_eval = evaluate_task_dependencies(t, today=ref_today)
        open_downstream = [
            d for d in t.dependent_tasks.all() if d.task.status not in TERMINAL_STATUSES
        ]

        row = {
            "id": str(t.id),
            "title": t.title,
            "project": str(t.project_id),
            "project_code": t.project.code,
            "project_name": t.project.name,
            "company_code": comp.code,
            "status": t.status,
            "priority": t.priority,
            "progress": float(t.progress),
            "start_date": t.start_date.isoformat() if t.start_date else None,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "task_owner_name": t.task_owner.full_name if t.task_owner else None,
            "is_dependency_blocked": dep_eval["is_dependency_blocked"],
            "blocking_prerequisites_count": dep_eval["blocking_prerequisites_count"],
            "downstream_dependents_count": len(open_downstream),
            **metrics,
        }

        if metrics["is_overdue"]:
            overdue_items.append(row)
            if metrics["overdue_severity"] == "Critical":
                critical_overdue_count += 1
            elif metrics["overdue_severity"] == "High":
                high_overdue_count += 1
            else:
                warning_overdue_count += 1
            for d in open_downstream:
                at_risk_dependent_ids.add(d.task_id)
        elif metrics["due_status"] == "Due Today":
            due_today_items.append(row)
        elif metrics["due_status"] == "Due Soon":
            due_soon_items.append(row)

        if dep_eval["is_dependency_blocked"]:
            dependency_blocked_items.append(row)

    overdue_items.sort(key=lambda r: (-r["days_overdue"], r["due_date"] or ""))
    due_soon_items.sort(key=lambda r: (r["days_remaining"] or 999, r["due_date"] or ""))

    primary_cal = next(iter(cal_cache.values()), get_company_working_calendar(None))

    return {
        "as_of_date": ref_today.isoformat(),
        "working_calendar": serialize_working_calendar(primary_cal),
        "kpis": {
            "total_evaluated": len(tasks),
            "overdue_count": len(overdue_items),
            "critical_overdue_count": critical_overdue_count,
            "high_overdue_count": high_overdue_count,
            "warning_overdue_count": warning_overdue_count,
            "due_today_count": len(due_today_items),
            "due_soon_count": len(due_soon_items),
            "dependency_blocked_count": len(dependency_blocked_items),
            "at_risk_dependent_count": len(at_risk_dependent_ids),
        },
        "overdue_tasks": overdue_items[:50],
        "due_today_tasks": due_today_items[:50],
        "due_soon_tasks": due_soon_items[:50],
        "dependency_blocked_tasks": dependency_blocked_items[:50],
    }


@transaction.atomic
def check_and_notify_deadlines_service(
    *,
    tasks_queryset,
    today: date | None = None,
    dedupe_hours: int = 24,
) -> dict:
    """
    SRS §7.10, §13.1, §20.2 & §22 Rules #10, #17:
    Scans open tasks for overdue deadlines, upcoming deadlines (within each company's
    `deadline_reminder_days`), and downstream dependency delay impact; dispatches deduplicated
    notifications (`TASK_OVERDUE`, `UPCOMING_DEADLINE`, `DEPENDENCY_DELAY_WARNING`) with
    `NotificationDelivery` records.
    """
    ref_today = today or timezone.now().date()
    open_tasks = list(
        tasks_queryset.exclude(status__in=list(TERMINAL_STATUSES))
        .filter(due_date__isnull=False)
        .select_related("project", "project__company", "project__project_manager", "task_owner")
        .prefetch_related("assignments", "dependent_tasks__task")
    )

    cal_cache: dict = {}
    overdue_found = 0
    upcoming_found = 0
    dependency_warnings_created = 0
    notifications_created = 0
    skipped_duplicates = 0
    alerts = []

    for task in open_tasks:
        comp = task.project.company
        if comp.id not in cal_cache:
            cal_cache[comp.id] = get_company_working_calendar(comp)
        cal = cal_cache[comp.id]
        metrics = compute_task_schedule_metrics(task, today=ref_today, calendar_cfg=cal)
        reminder_days = cal["deadline_reminder_days"]

        # Collect assignees + owner
        assignee_ids = set()
        if task.task_owner_id:
            assignee_ids.add(task.task_owner_id)
        for a in task.assignments.all():
            if a.assignment_type == TaskAssignment.AssignmentType.USER and a.status in (
                "Active",
                "Acknowledged",
            ) and a.user_id:
                assignee_ids.add(a.user_id)

        if metrics["is_overdue"]:
            overdue_found += 1
            recipients = set(assignee_ids)
            if task.project.project_manager_id:
                recipients.add(task.project.project_manager_id)

            sent_here = 0
            for uid in recipients:
                notif = _dispatch_notification_with_delivery(
                    user_id=uid,
                    notification_type="TASK_OVERDUE",
                    title=f"Overdue Task ({metrics['overdue_severity']}): {task.title}",
                    message=(
                        f"Task '{task.title}' in project '{task.project.code}' is overdue by "
                        f"{metrics['days_overdue']} calendar day(s) "
                        f"({metrics['working_days_overdue']} working day(s)). Due date was {task.due_date}."
                    ),
                    related_project=task.project,
                    related_task=task,
                    dedupe_hours=dedupe_hours,
                )
                if notif:
                    notifications_created += 1
                    sent_here += 1
                else:
                    skipped_duplicates += 1

            # Also warn managers if overdue task delays downstream dependents
            dep_notifs = notify_dependency_delay_impact(
                task=task,
                trigger_reason=f"Overdue by {metrics['days_overdue']}d ({metrics['working_days_overdue']} working d)",
                dedupe_hours=dedupe_hours,
            )
            dependency_warnings_created += len(dep_notifs)
            notifications_created += len(dep_notifs)

            alerts.append(
                {
                    "task_id": str(task.id),
                    "title": task.title,
                    "project_code": task.project.code,
                    "alert_type": "TASK_OVERDUE",
                    "severity": metrics["overdue_severity"],
                    "days_overdue": metrics["days_overdue"],
                    "working_days_overdue": metrics["working_days_overdue"],
                    "notifications_sent": sent_here + len(dep_notifs),
                }
            )

        elif (
            metrics["days_remaining"] is not None
            and 0 <= metrics["days_remaining"] <= reminder_days
        ):
            upcoming_found += 1
            sent_here = 0
            when_label = (
                "today"
                if metrics["days_remaining"] == 0
                else f"in {metrics['days_remaining']} calendar day(s) ({metrics['working_days_remaining']} working day(s))"
            )
            for uid in assignee_ids:
                notif = _dispatch_notification_with_delivery(
                    user_id=uid,
                    notification_type="UPCOMING_DEADLINE",
                    title=f"Upcoming Deadline: {task.title}",
                    message=(
                        f"Reminder: Task '{task.title}' in project '{task.project.code}' is due "
                        f"{when_label} ({task.due_date})."
                    ),
                    related_project=task.project,
                    related_task=task,
                    dedupe_hours=dedupe_hours,
                )
                if notif:
                    notifications_created += 1
                    sent_here += 1
                else:
                    skipped_duplicates += 1

            alerts.append(
                {
                    "task_id": str(task.id),
                    "title": task.title,
                    "project_code": task.project.code,
                    "alert_type": "UPCOMING_DEADLINE",
                    "severity": "Warning",
                    "days_remaining": metrics["days_remaining"],
                    "working_days_remaining": metrics["working_days_remaining"],
                    "notifications_sent": sent_here,
                }
            )

    return {
        "as_of_date": ref_today.isoformat(),
        "scanned_tasks": len(open_tasks),
        "overdue_tasks_found": overdue_found,
        "upcoming_deadlines_found": upcoming_found,
        "dependency_warnings_sent": dependency_warnings_created,
        "notifications_created": notifications_created,
        "skipped_duplicates": skipped_duplicates,
        "alerts": alerts[:100],
    }
