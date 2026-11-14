import uuid
from django.conf import settings
from django.db import models


class TaskReview(models.Model):
    class Decision(models.TextChoices):
        APPROVED = "Approved", "Approved"
        CHANGES_REQUESTED = "Changes Requested", "Changes Requested"
        REJECTED = "Rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.PROTECT,
        related_name="reviews",
        db_column="task_id",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="performed_reviews",
        db_column="reviewer_id",
    )
    decision = models.CharField(max_length=30, choices=Decision.choices)
    comments = models.TextField(null=True, blank=True)
    correction_reason = models.TextField(null=True, blank=True)
    reviewed_at = models.DateTimeField(auto_now_add=True)
    review_round = models.IntegerField(default=1)

    class Meta:
        db_table = "task_reviews"
        verbose_name = "Task Review"
        verbose_name_plural = "Task Reviews"
        ordering = ["-reviewed_at"]

    def __str__(self):
        return f"Review #{self.review_round} on {self.task.title}: {self.decision}"
