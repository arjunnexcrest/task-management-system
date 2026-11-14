from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (
    MilestoneDependencyViewSet,
    MilestoneTaskViewSet,
    ProjectDepartmentViewSet,
    ProjectMemberViewSet,
    ProjectMilestoneViewSet,
    ProjectPhaseViewSet,
    ProjectTeamViewSet,
    ProjectViewSet,
)

router = DefaultRouter()
router.register("members", ProjectMemberViewSet, basename="project-members")
router.register("departments", ProjectDepartmentViewSet, basename="project-departments")
router.register("teams", ProjectTeamViewSet, basename="project-teams")
router.register("phases", ProjectPhaseViewSet, basename="project-phases")
router.register("milestones", ProjectMilestoneViewSet, basename="project-milestones")
router.register("milestone-tasks", MilestoneTaskViewSet, basename="milestone-tasks")
router.register("milestone-dependencies", MilestoneDependencyViewSet, basename="milestone-dependencies")
router.register("", ProjectViewSet, basename="projects")

urlpatterns = [
    path("", include(router.urls)),
]
