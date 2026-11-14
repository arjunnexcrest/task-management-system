from django.contrib import admin
from .models import ProgressAttachment, ProgressUpdate, TimeEntry


@admin.register(ProgressUpdate)
class ProgressUpdateAdmin(admin.ModelAdmin):
    list_display = ("task", "author", "progress_percentage", "time_spent_minutes", "created_at")
    list_filter = ("created_at",)
    search_fields = ("task__title", "author__full_name")


@admin.register(ProgressAttachment)
class ProgressAttachmentAdmin(admin.ModelAdmin):
    list_display = ("progress_update", "attachment")


@admin.register(TimeEntry)
class TimeEntryAdmin(admin.ModelAdmin):
    list_display = ("task", "user", "entry_type", "duration_minutes", "approval_status", "created_at")
    list_filter = ("entry_type", "approval_status")
    search_fields = ("task__title", "user__full_name")
