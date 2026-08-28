from django.urls import path

from analytics import dashboard_views

urlpatterns = [
    path("", dashboard_views.dashboards),
    path("<slug:slug>/", dashboard_views.dashboard_detail),
]
