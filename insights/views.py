"""Master Dashboard API (/api/insights/)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.http import JsonResponse
from django.urls import path
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from accounts.authz import protect
from insights import periods
from insights.drill import cancellation_row, drill, plan_row
from insights.metrics import KPIS, SCORECARD, Metrics, what_changed
from insights.models import (
    PAUSED_STATUSES,
    PLAN_STATUS_CHOICES,
    PRICING_NONE,
    STATUS_ACTIVE,
    MrrChange,
    RecurringPlan,
)
from insights.mrr import refresh_current_snapshot, tracking_start
from insights.trends import TREND_METRICS, series
from integrations.models import Integration
from operations.models import (
    CANCELLATION_JOB,
    CANCELLATION_REASON_CHOICES,
    CANCELLATION_REASON_GROUPS,
    CANCELLATION_VISIT,
    DIVISION_CHOICES,
    REVENUE_CATEGORY_CHOICES,
    CancellationRecord,
)

DIVISION_KEYS = {key for key, _ in DIVISION_CHOICES}
REASON_KEYS = {key for key, _ in CANCELLATION_REASON_CHOICES}
STATUS_KEYS = {key for key, _ in PLAN_STATUS_CHOICES}
GROUP_LABELS = {"customer": "Customer", "internal": "Internal", "external": "External", "other": "Other", "": "No reason yet"}


class InsightsEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


def respond(payload, status=200):
    return JsonResponse({"ok": True, **payload} if status == 200 else payload, status=status, encoder=InsightsEncoder)


def error(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


def _integration():
    return Integration.objects.filter(status=Integration.STATUS_ACTIVE).order_by("-updated_at").first()


def _context(request):
    integration = _integration()
    if not integration:
        return None, None, None
    division = request.GET.get("division") or ""
    if division not in DIVISION_KEYS:
        division = ""
    period = periods.from_request(request)
    return Metrics(integration, division), period, periods.comparisons(period)


def _period_payload(period, comps):
    return {
        "period": period.as_dict(),
        "comparisons": {name: other.as_dict() for name, other in comps.items()},
        "navigation": {"previous": period.step(-1).as_dict(), "next": period.step(1).as_dict()},
    }


def _cards(metrics, keys, period, comps):
    return [metrics.card(key, period, comps) for key in keys]


def _no_integration():
    return error("Connect Jobber to see the Master Dashboard.", status=409)


@require_GET
def meta(request):
    integration = _integration()
    metrics = Metrics(integration) if integration else None
    return respond(
        {
            "divisions": [{"key": k, "label": v} for k, v in DIVISION_CHOICES],
            "revenue_categories": [{"key": k, "label": v} for k, v in REVENUE_CATEGORY_CHOICES],
            "cancellation_reasons": [
                {"key": k, "label": v, "group": CANCELLATION_REASON_GROUPS.get(k, "other")} for k, v in CANCELLATION_REASON_CHOICES
            ],
            "plan_statuses": [{"key": k, "label": v} for k, v in PLAN_STATUS_CHOICES],
            "trend_metrics": [{"key": k, "label": KPIS[k]["label"], "available": not KPIS[k]["note"]} for k in TREND_METRICS],
            "data_starts": metrics.data_starts() if metrics else {},
            "mrr_tracking_start": tracking_start() if integration else None,
            "today": timezone.localdate(),
        }
    )


@require_GET
def overview(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    against = request.GET.get("compare") if request.GET.get("compare") in comps else "previous"
    return respond(
        {
            **_period_payload(period, comps),
            "scorecard": _cards(metrics, SCORECARD, period, comps),
            "what_changed": {"against": against, "items": what_changed(metrics, period, comps[against])},
        }
    )


def _rows(metrics, field, choices, period, comps):
    current = metrics.revenue_by(field, period)
    others = {name: metrics.revenue_by(field, other) for name, other in comps.items()}
    total = sum(current.values(), Decimal(0))
    rows = []
    for key, label in choices:
        value = current.get(key, Decimal(0))
        rows.append(
            {
                "key": key,
                "label": label,
                "current": value,
                "share": (value / total * 100).quantize(Decimal("0.1")) if total else None,
                **{name: (vals.get(key, Decimal(0)) if metrics._has("revenue", comps[name]) else None) for name, vals in others.items()},
            }
        )
    return rows


@require_GET
def revenue(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    return respond(
        {
            **_period_payload(period, comps),
            "cards": _cards(
                metrics,
                ["revenue", "invoiced_revenue", "discounts", "completed_visit_value", "payments_received", "avg_revenue_per_visit"],
                period,
                comps,
            ),
            "categories": _rows(metrics, "revenue_category", REVENUE_CATEGORY_CHOICES, period, comps),
            "divisions": _rows(metrics, "division", DIVISION_CHOICES, period, comps),
            "profitability": _cards(
                metrics,
                ["revenue", "cogs", "gross_profit", "gross_margin", "payroll", "payroll_pct", "operating_expenses", "net_profit", "net_margin"],
                period,
                comps,
            ),
            "concentration": _cards(metrics, ["top5_share", "top10_share"], period, comps),
            "receivables": _cards(metrics, ["ar_balance", "ar_overdue"], period, comps),
            "top_clients": metrics.top_clients(period, 10),
        }
    )


@require_GET
def clients(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    return respond(
        {
            **_period_payload(period, comps),
            "growth": _cards(
                metrics,
                ["active_clients_start", "new_clients", "returning_clients", "lost_clients", "net_client_growth", "active_clients"],
                period,
                comps,
            ),
            "recurring": _cards(
                metrics,
                [
                    "active_recurring_clients",
                    "paused_clients",
                    "recurring_retention",
                    "new_recurring_jobs",
                    "cancelled_recurring_jobs",
                    "net_recurring_jobs",
                ],
                period,
                comps,
            ),
            "sales": _cards(metrics, ["leads", "lead_conversion"], period, comps),
        }
    )


@require_GET
def mrr(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    result = metrics.mrr(period)
    steps = []
    if result.available:
        expansion_ok = period.end >= result.expansion_tracked_from
        steps = [
            {"key": "mrr_starting", "label": "Starting MRR", "value": result.starting, "type": "total"},
            {"key": "mrr_new", "label": "New", "value": result.totals["new"], "type": "delta"},
            {"key": "mrr_reactivated", "label": "Reactivated", "value": result.totals["reactivated"], "type": "delta"},
            {"key": "mrr_expansion", "label": "Expansion", "value": result.totals["expansion"] if expansion_ok else None, "type": "delta"},
            {"key": "mrr_contraction", "label": "Contraction", "value": result.totals["contraction"] if expansion_ok else None, "type": "delta"},
            {"key": "mrr_paused", "label": "Paused / resumed", "value": result.totals["paused"] + result.totals["resumed"], "type": "delta"},
            {"key": "mrr_churned", "label": "Churned", "value": result.totals["churned"], "type": "delta"},
        ]
        if result.totals["untracked"]:
            steps.append({"key": "mrr_untracked", "label": "Before tracking", "value": result.totals["untracked"], "type": "delta"})
        steps.append({"key": "mrr", "label": "Ending MRR", "value": result.ending, "type": "total"})
    return respond(
        {
            **_period_payload(period, comps),
            "cards": _cards(
                metrics,
                [
                    "mrr_starting",
                    "mrr_new",
                    "mrr_expansion",
                    "mrr_reactivated",
                    "mrr_contraction",
                    "mrr_churned",
                    "mrr_paused_net",
                    "mrr_net_new",
                    "mrr",
                    "mrr_paused",
                    "active_recurring_clients",
                    "paused_clients",
                ],
                period,
                comps,
            ),
            "waterfall": steps,
            "tracking_start": tracking_start(),
            "expansion_tracked_from": result.expansion_tracked_from if result.available else None,
        }
    )


@require_GET
def mrr_plans(request):
    integration = _integration()
    if not integration:
        return _no_integration()
    qs = RecurringPlan.objects.filter(integration=integration).exclude(client__is_internal=True).select_related("job")
    division = request.GET.get("division") or ""
    if division in DIVISION_KEYS:
        qs = qs.filter(division=division)
    status = request.GET.get("status") or "current"
    if status == "no_price":
        qs = qs.filter(pricing_source=PRICING_NONE).filter(
            Q(status_override__in=[STATUS_ACTIVE, *PAUSED_STATUSES]) | Q(status_override="", detected_status=STATUS_ACTIVE)
        )
    elif status == "current":
        qs = qs.filter(Q(status_override__in=[STATUS_ACTIVE, *PAUSED_STATUSES]) | Q(status_override="", detected_status=STATUS_ACTIVE))
    elif status in STATUS_KEYS:
        qs = qs.filter(Q(status_override=status) | Q(status_override="", detected_status=status))
    search = (request.GET.get("q") or "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(service__icontains=search))
    page = Paginator(qs.order_by("name", "id"), 50).get_page(request.GET.get("page") or 1)
    return respond(
        {
            "count": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "results": [plan_row(plan) for plan in page.object_list],
        }
    )


def _body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return {}


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"Invalid date: {value}")


@csrf_exempt
@require_http_methods(["PATCH"])
def mrr_plan_update(request, pk):
    integration = _integration()
    plan = RecurringPlan.objects.filter(integration=integration, pk=pk).select_related("job").first()
    if not plan:
        return error("Plan not found.", status=404)
    body = _body(request)
    before_status, before_value = plan.status, plan.effective_monthly_value
    try:
        if "status_override" in body:
            value = body["status_override"] or ""
            if value and value not in STATUS_KEYS:
                return error("Unknown status.")
            plan.status_override = value
        for field in ("pause_reason", "manual_note"):
            if field in body:
                setattr(plan, field, str(body[field] or "")[:255 if field == "pause_reason" else 5000])
        for field in ("pause_start", "expected_return"):
            if field in body:
                setattr(plan, field, _date(body[field]))
        if "manual_monthly_value" in body:
            raw = body["manual_monthly_value"]
            plan.manual_monthly_value = None if raw in (None, "") else Decimal(str(raw)).quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation) as exc:
        return error(str(exc) or "Invalid value.")

    if plan.status in PAUSED_STATUSES and not plan.pause_start:
        plan.pause_start = timezone.localdate()
    plan.updated_by = request.user
    plan.save()

    today = timezone.localdate()
    if plan.status != before_status:
        MrrChange.objects.create(
            plan=plan, changed_on=today, change_type=MrrChange.TYPE_STATUS, old_value=before_status, new_value=plan.status,
            note=plan.pause_reason or plan.manual_note, created_by=request.user,
        )
    if plan.effective_monthly_value != before_value:
        MrrChange.objects.create(
            plan=plan, changed_on=today, change_type=MrrChange.TYPE_CORRECTION, old_value=str(before_value),
            new_value=str(plan.effective_monthly_value), monthly_delta=plan.effective_monthly_value - before_value,
            note=plan.manual_note, created_by=request.user,
        )
    refresh_current_snapshot(plan, today)
    return respond({"plan": plan_row(plan)})


@require_GET
def cancellations(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    current = metrics.reason_breakdown(period) if metrics._has("cancellations", period) else {}
    others = {
        name: metrics.reason_breakdown(other) if metrics._has("cancellations", other) else None for name, other in comps.items()
    }
    reasons = []
    for key, label in [*CANCELLATION_REASON_CHOICES, ("", "No reason yet")]:
        row = current.get(key, {"count": 0, "value": Decimal(0)})
        reasons.append(
            {
                "key": key,
                "label": label,
                "group": CANCELLATION_REASON_GROUPS.get(key, ""),
                "count": row["count"],
                "value": row["value"],
                **{name: (vals.get(key, {"count": 0})["count"] if vals is not None else None) for name, vals in others.items()},
            }
        )
    groups = {}
    for row in reasons:
        group = groups.setdefault(row["group"], {"key": row["group"], "label": GROUP_LABELS[row["group"]], "count": 0, "value": Decimal(0)})
        group["count"] += row["count"]
        group["value"] += row["value"]
    records = metrics.cancellations(period).select_related("client").order_by("-task_date", "-id")
    return respond(
        {
            **_period_payload(period, comps),
            "data_start": metrics.data_starts().get("cancellations"),
            "cards": _cards(
                metrics,
                [
                    "cancelled_visits",
                    "cancelled_visits_value",
                    "cancellations_internal",
                    "cancelled_jobs",
                    "cancelled_jobs_value",
                    "service_cancellation_forms",
                ],
                period,
                comps,
            ),
            "service_reasons": metrics.service_cancellation_reasons(period),
            "reasons": reasons,
            "groups": [groups[key] for key in ("customer", "internal", "external", "other", "") if key in groups],
            "records": [cancellation_row(record) for record in records[:500]],
        }
    )


@csrf_exempt
@require_http_methods(["PATCH"])
def cancellation_update(request, pk):
    record = CancellationRecord.objects.filter(integration=_integration(), pk=pk).select_related("client").first()
    if not record:
        return error("Cancellation not found.", status=404)
    body = _body(request)
    if "reason" in body:
        reason = body["reason"] or ""
        if reason and reason not in REASON_KEYS:
            return error("Unknown reason.")
        record.reason = reason
    if "reason_notes" in body:
        record.reason_notes = str(body["reason_notes"] or "")[:5000]
    record.reason_updated_at = timezone.now()
    record.reason_updated_by = request.user
    record.save(update_fields=["reason", "reason_notes", "reason_updated_at", "reason_updated_by", "updated_at"])
    return respond({"record": cancellation_row(record)})


@require_GET
def quality(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    return respond(
        {
            **_period_payload(period, comps),
            "redos": _cards(metrics, ["touch_ups", "touch_up_rate"], period, comps),
            "feedback": _cards(metrics, ["feedback_score", "feedback_responses", "feedback_participation"], period, comps),
            "complaints": _cards(
                metrics, ["complaints", "complaint_rate", "complaints_open", "complaint_resolution_days"], period, comps
            ),
            "google": _cards(metrics, ["google_rating", "google_reviews", "google_new_reviews", "google_five_star_new"], period, comps),
            "complaint_types": metrics.complaint_types(period) if metrics._has("complaints", period) else [],
            "people": metrics.employee_table(period),
        }
    )


@require_GET
def operations(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    return respond(
        {
            **_period_payload(period, comps),
            "data_start": metrics.data_starts().get("hours"),
            "work": _cards(
                metrics,
                ["visits_completed", "labour_hours", "revenue_per_labour_hour", "overtime_hours", "drive_time_hours"],
                period,
                comps,
            ),
            "people": _cards(metrics, ["absence_days", "late_arrivals", "cancellations_internal"], period, comps),
            "payroll": _cards(metrics, ["payroll", "payroll_pct"], period, comps),
            "employees": metrics.employee_table(period),
        }
    )


@require_GET
def trend(request):
    integration = _integration()
    if not integration:
        return _no_integration()
    division = request.GET.get("division") or ""
    metrics = Metrics(integration, division if division in DIVISION_KEYS else "")
    grain = request.GET.get("grain") if request.GET.get("grain") in ("month", "quarter", "year") else None
    range_key = request.GET.get("range") if request.GET.get("range") in ("12m", "3y", "5y", "lifetime", "custom") else "12m"
    keys = [k for k in (request.GET.get("metrics") or request.GET.get("metric") or "revenue").split(",") if k in KPIS][:4]
    return respond(
        {
            "series": [
                series(metrics, key, range_key, grain, start=periods._parse_date(request.GET.get("from")), end=periods._parse_date(request.GET.get("to")))
                for key in keys
            ]
        }
    )


@require_GET
def drill_view(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    which = request.GET.get("of") or "current"
    if which in comps:
        period = comps[which]
    key = request.GET.get("metric") or ""
    result = drill(metrics, key, period, category=request.GET.get("category") or "")
    return respond({"metric": key, "period": period.as_dict(), **result})


urlpatterns = [
    path("meta/", protect(meta)),
    path("overview/", protect(overview)),
    path("revenue/", protect(revenue)),
    path("clients/", protect(clients)),
    path("mrr/", protect(mrr)),
    path("mrr/plans/", protect(mrr_plans)),
    path("mrr/plans/<int:pk>/", protect(mrr_plan_update, staff=True)),
    path("cancellations/", protect(cancellations)),
    path("cancellations/<int:pk>/", protect(cancellation_update, staff=True)),
    path("quality/", protect(quality)),
    path("operations/", protect(operations)),
    path("trend/", protect(trend)),
    path("drill/", protect(drill_view)),
]
