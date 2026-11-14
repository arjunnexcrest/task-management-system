from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    AttachmentViewSet,
    ChecklistItemViewSet,
    ChecklistViewSet,
    CommentAttachmentViewSet,
    CommentMentionViewSet,
    TaskAssignmentViewSet,
    TaskAttachmentViewSet,
    TaskCommentViewSet,
    TaskDependencyViewSet,
    TaskTagAssignmentViewSet,
    TaskTagViewSet,
    TaskViewSet,
)

router = DefaultRouter()
router.register("assignments", TaskAssignmentViewSet, basename="task-assignments")
router.register("dependencies", TaskDependencyViewSet, basename="task-dependencies")
router.register("tags", TaskTagViewSet, basename="task-tags")
router.register("tag-assignments", TaskTagAssignmentViewSet, basename="task-tag-assignments")
router.register("checklists", ChecklistViewSet, basename="checklists")
router.register("checklist-items", ChecklistItemViewSet, basename="checklist-items")
router.register("comments", TaskCommentViewSet, basename="task-comments")
router.register("comment-mentions", CommentMentionViewSet, basename="comment-mentions")
router.register("attachments", AttachmentViewSet, basename="attachments")
router.register("task-attachments", TaskAttachmentViewSet, basename="task-attachments")
router.register("comment-attachments", CommentAttachmentViewSet, basename="comment-attachments")
router.register("", TaskViewSet, basename="tasks")

urlpatterns = [
    path("", include(router.urls)),
]
