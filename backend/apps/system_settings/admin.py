from django.contrib import admin
from .models import SystemSetting, WorkflowStatus, WorkflowTransition


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ("setting_key", "company", "updated_by", "updated_at")
    list_filter = ("company",)


@admin.register(WorkflowStatus)
class WorkflowStatusAdmin(admin.ModelAdmin):
    list_display = ("entity_type", "display_name", "status_code", "company", "sort_order", "is_active")
    list_filter = ("entity_type", "is_active")


@admin.register(WorkflowTransition)
class WorkflowTransitionAdmin(admin.ModelAdmin):
    list_display = ("entity_type", "from_status", "to_status", "role", "company")
    list_filter = ("entity_type",)
