"""
rbac/roles.py

Single source of truth for SRS system roles.
Easy to extend with new roles as modules grow.
"""
from django.db import models


class SystemRole(models.TextChoices):
    CEO = "CEO", "CEO"
    COMPANY_IN_CHARGE = "CompanyInCharge", "Company In-Charge"
    PROJECT_MANAGER = "ProjectManager", "Project Manager"
    DEPARTMENT_HEAD = "DepartmentHead", "Department Head"
    TEAM_LEADER = "TeamLeader", "Team Leader"
    EMPLOYEE = "Employee", "Employee"
