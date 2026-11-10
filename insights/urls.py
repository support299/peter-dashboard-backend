from django.urls import path

from accounts.authz import protect
from insights import manage_views as m
from insights import manager_views as mv
from insights.views import urlpatterns as dashboard_urls

urlpatterns = [
    *dashboard_urls,
    path("manager/", protect(mv.manager)),
    path("manager/history/", protect(mv.manager_history)),
    path("manager/capacity-check/", protect(mv.capacity_check, staff=True)),
    path("alerts/", protect(m.alerts)),
    path("alerts/<int:pk>/", protect(m.alert_detail, staff=True)),
    path("rules/", protect(m.rules, staff=True)),
    path("rules/<int:pk>/", protect(m.rule_detail, staff=True)),
    path("rules/test/", protect(m.rule_test, staff=True)),
    path("rules/lines/", protect(m.rule_lines, staff=True)),
    path("rules/apply/", protect(m.rules_apply, staff=True)),
    path("mrr/old-tab/", protect(m.old_tab)),
    path("mrr/old-tab/<int:pk>/", protect(m.old_tab_link, staff=True)),
    path("mrr/plan-search/", protect(m.plan_search)),
]
