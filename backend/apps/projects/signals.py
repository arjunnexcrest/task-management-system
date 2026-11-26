"""
Project domain event hooks.
Business state rollups and notifications are orchestrated explicitly inside
atomic service functions in `apps.projects.services` (DB Design §7 Rule #6)
to avoid hidden signal cascades or duplicate notifications.
"""

from apps.projects.services import (
    recalculate_milestone_progress_service,
    recalculate_project_progress_service,
)

__all__ = [
    "recalculate_milestone_progress_service",
    "recalculate_project_progress_service",
]
