from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/accounts/", include("accounts.urls")),
    path("api/jobber/", include("integrations.urls")),
    path("api/operations/", include("operations.urls")),
    path("api/admin-internal/", include("analytics.urls")),
    path("api/pricing-calculator/", include("analytics.pricing_urls")),
    path("api/dashboards/", include("analytics.dashboard_urls")),
]
