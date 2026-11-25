"""
Task domain event hooks.
Task rollups, assignment notifications, dependency warnings, and workflow
audit events are orchestrated explicitly inside atomic service functions in
`apps.tasks.services` (DB Design §7 Rule #6) for predictable execution order.
"""

from apps.tasks.services import (
    check_and_notify_deadlines_service,
    transition_task_status_service,
)

__all__ = [
    "check_and_notify_deadlines_service",
    "transition_task_status_service",
]
