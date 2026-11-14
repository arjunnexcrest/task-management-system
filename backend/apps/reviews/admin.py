from django.contrib import admin
from .models import TaskReview


@admin.register(TaskReview)
class TaskReviewAdmin(admin.ModelAdmin):
    list_display = ("task", "reviewer", "decision", "review_round", "reviewed_at")
    list_filter = ("decision",)
    search_fields = ("task__title", "reviewer__full_name")
