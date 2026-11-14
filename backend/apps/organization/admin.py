from django.contrib import admin
from .models import Department, DepartmentMembership, ReportingRelationship, Team, TeamMembership


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "company", "status", "created_at")
    list_filter = ("status", "company")
    search_fields = ("name", "code")


@admin.register(DepartmentMembership)
class DepartmentMembershipAdmin(admin.ModelAdmin):
    list_display = ("department", "user", "is_head", "status", "start_date", "end_date")
    list_filter = ("is_head", "status")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "department", "status", "created_at")
    list_filter = ("status", "department")
    search_fields = ("name", "code")


@admin.register(TeamMembership)
class TeamMembershipAdmin(admin.ModelAdmin):
    list_display = ("team", "user", "is_leader", "status", "joined_at", "left_at")
    list_filter = ("is_leader", "status")


@admin.register(ReportingRelationship)
class ReportingRelationshipAdmin(admin.ModelAdmin):
    list_display = ("company", "employee", "manager", "start_date", "end_date")
    list_filter = ("company",)
