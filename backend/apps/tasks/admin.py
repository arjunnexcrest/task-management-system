from django.contrib import admin
from .models import (
    Attachment,
    Checklist,
    ChecklistItem,
    CommentAttachment,
    CommentMention,
    Task,
    TaskAssignment,
    TaskAttachment,
    TaskComment,
    TaskDependency,
    TaskTag,
    TaskTagAssignment,
)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "parent_task", "phase", "priority", "status", "progress", "task_owner", "due_date")
    list_filter = ("status", "priority", "progress_method", "approval_required", "project")
    search_fields = ("title", "description")


@admin.register(TaskAssignment)
class TaskAssignmentAdmin(admin.ModelAdmin):
    list_display = ("task", "assignment_type", "user", "team", "assigned_by", "status", "assigned_at", "ended_at")
    list_filter = ("assignment_type", "status")
    search_fields = ("task__title", "user__email", "team__name")


@admin.register(TaskDependency)
class TaskDependencyAdmin(admin.ModelAdmin):
    list_display = ("task", "depends_on_task", "dependency_type", "created_at")
    list_filter = ("dependency_type",)


@admin.register(TaskTag)
class TaskTagAdmin(admin.ModelAdmin):
    list_display = ("name", "id")
    search_fields = ("name",)


@admin.register(TaskTagAssignment)
class TaskTagAssignmentAdmin(admin.ModelAdmin):
    list_display = ("task", "tag")
    list_filter = ("tag",)


@admin.register(Checklist)
class ChecklistAdmin(admin.ModelAdmin):
    list_display = ("title", "task", "sort_order", "created_by", "created_at")


@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    list_display = ("title", "checklist", "sort_order", "is_completed", "completed_by", "completed_at")
    list_filter = ("is_completed",)


@admin.register(TaskComment)
class TaskCommentAdmin(admin.ModelAdmin):
    list_display = ("task", "author", "parent_comment", "created_at")
    search_fields = ("content", "task__title", "author__email")


@admin.register(CommentMention)
class CommentMentionAdmin(admin.ModelAdmin):
    list_display = ("comment", "mentioned_user")


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("file_name", "mime_type", "file_size_bytes", "uploaded_by", "uploaded_at")
    list_filter = ("mime_type",)
    search_fields = ("file_name", "storage_key")


@admin.register(TaskAttachment)
class TaskAttachmentAdmin(admin.ModelAdmin):
    list_display = ("task", "attachment")


@admin.register(CommentAttachment)
class CommentAttachmentAdmin(admin.ModelAdmin):
    list_display = ("comment", "attachment")
