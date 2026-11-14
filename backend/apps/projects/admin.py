from django.contrib import admin
from .models import (
    MilestoneDependency,
    MilestoneTask,
    Project,
    ProjectDepartment,
    ProjectMember,
    ProjectMilestone,
    ProjectPhase,
    ProjectTeam,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "company", "project_manager", "priority", "status", "progress", "start_date", "planned_completion_date")
    list_filter = ("status", "priority", "company")
    search_fields = ("name", "code", "description")


@admin.register(ProjectMember)
class ProjectMemberAdmin(admin.ModelAdmin):
    list_display = ("project", "user", "role_in_project", "status", "joined_at", "left_at")
    list_filter = ("status", "project")
    search_fields = ("project__code", "user__email", "user__full_name")


@admin.register(ProjectDepartment)
class ProjectDepartmentAdmin(admin.ModelAdmin):
    list_display = ("project", "department", "created_at")
    list_filter = ("project", "department")


@admin.register(ProjectTeam)
class ProjectTeamAdmin(admin.ModelAdmin):
    list_display = ("project", "team", "created_at")
    list_filter = ("project", "team")


@admin.register(ProjectPhase)
class ProjectPhaseAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "owner", "status", "progress", "start_date", "end_date")
    list_filter = ("status", "project")


@admin.register(ProjectMilestone)
class ProjectMilestoneAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "owner", "target_date", "status", "progress", "approval_required")
    list_filter = ("status", "approval_required", "project")
    search_fields = ("title", "description")


@admin.register(MilestoneTask)
class MilestoneTaskAdmin(admin.ModelAdmin):
    list_display = ("milestone", "task")
    list_filter = ("milestone",)


@admin.register(MilestoneDependency)
class MilestoneDependencyAdmin(admin.ModelAdmin):
    list_display = ("milestone", "depends_on_milestone")
