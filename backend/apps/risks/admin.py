from django.contrib import admin
from .models import Blocker, Issue, Risk


@admin.register(Blocker)
class BlockerAdmin(admin.ModelAdmin):
    list_display = ("task", "category", "impact", "responsible_person", "resolution_status", "reported_at")
    list_filter = ("resolution_status", "impact")


@admin.register(Risk)
class RiskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "probability", "impact", "severity", "owner", "status")
    list_filter = ("status", "severity")


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "task", "priority", "owner", "status", "reported_at")
    list_filter = ("status", "priority")
