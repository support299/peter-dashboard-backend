from django.urls import path

from accounts.authz import protect
from analytics import dashboard_views

urlpatterns = [
    path("", protect(dashboard_views.dashboards)),
    path("<slug:slug>/", protect(dashboard_views.dashboard_detail)),
]
