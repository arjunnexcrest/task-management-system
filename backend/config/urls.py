from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # App URL includes will be wired by developers as APIs are implemented.
    # path("api/accounts/", include("apps.accounts.urls")),
    # path("api/companies/", include("apps.companies.urls")),
    # path("api/organization/", include("apps.organization.urls")),
    # path("api/rbac/", include("apps.rbac.urls")),
    # path("api/projects/", include("apps.projects.urls")),
    # path("api/tasks/", include("apps.tasks.urls")),
    # path("api/tracking/", include("apps.tracking.urls")),
    # path("api/reviews/", include("apps.reviews.urls")),
    # path("api/notifications/", include("apps.notifications.urls")),
    # path("api/dashboards/", include("apps.dashboards.urls")),
    # path("api/reports/", include("apps.reports.urls")),
    # path("api/risks/", include("apps.risks.urls")),
    # path("api/settings/", include("apps.system_settings.urls")),
]
