"""
Task module RBAC permission exports (SRS §2 & §7).
Centralized implementations live in `apps.common.permissions`.
"""

from apps.common.permissions import (
    TaskAssignmentPermission,
    TaskDependencyPermission,
    TaskManagementPermission,
)

__all__ = [
    "TaskManagementPermission",
    "TaskAssignmentPermission",
    "TaskDependencyPermission",
]
