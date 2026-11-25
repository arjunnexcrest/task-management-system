"""
Project module RBAC permission exports (SRS §2 & §6).
Centralized implementations live in `apps.common.permissions`.
"""

from apps.common.permissions import (
    ProjectManagementPermission,
    ProjectParticipationWritePermission,
    ProjectPlanningWritePermission,
)

__all__ = [
    "ProjectManagementPermission",
    "ProjectParticipationWritePermission",
    "ProjectPlanningWritePermission",
]
