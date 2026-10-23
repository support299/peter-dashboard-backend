"""Long-term trend series: last 12 months … lifetime, grouped by month, quarter or year."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Avg, Count, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from analytics.hub_models import HubComplaint, HubReview, HubWorkHours
from insights.metrics import KPIS, Metrics
from insights.models import PAUSED_STATUSES, MrrSnapshot
from insights.periods import add_months, aware, month_start, months_between
from operations.models import CANCELLATION_VISIT, CancellationRecord, InvoiceLineItem, Visit

RANGES = {"12m": 12, "3y": 36, "5y": 60}
TREND_METRICS = [
    "revenue",
    "gross_profit",
    "net_profit",
    "mrr",
    "active_recurring_clients",
    "payroll_pct",
    "avg_revenue_per_visit",
    "visits_completed",
    "new_clients",
    "cancelled_visits_value",
    "touch_ups",
    "labour_hours",
    "complaints",
    "feedback_score",
    "google_new_reviews",
]
# Balances are read at the end of each bucket; flows are summed across it.
POINT_IN_TIME = {"mrr", "active_recurring_clients"}
# Averaged across the months of a bucket (simple mean of monthly averages).
AVERAGES = {"feedback_score"}


def _month_key(value) -> date:
    if hasattr(value, "tzinfo") and value.tzinfo is not None:
        value = timezone.localtime(value)
    return date(value.year, value.month, 1)


def _by_month(qs, field, agg):
    rows = qs.annotate(m=TruncMonth(field)).values("m").annotate(v=agg).order_by()
    return {_month_key(row["m"]): Decimal(row["v"] or 0) for row in rows if row["m"]}


def resolve_range(metrics: Metrics, range_key: str, start: date | None, end: date | None):
    today = timezone.localdate()
    last = month_start(end or today)
    if range_key == "custom" and start:
        first = month_start(start)
    elif range_key == "lifetime":
        first_data = metrics.data_starts().get("revenue") or metrics.data_starts().get("visits") or today
        first = month_start(first_data)
    else:
        first = add_months(month_start(today), -(RANGES.get(range_key, 12) - 1))
        last = month_start(today)
    return first, max(first, last)


def default_grain(range_key: str) -> str:
    return {"12m": "month", "3y": "quarter"}.get(range_key, "year")


def monthly_series(metrics: Metrics, key: str, first: date, last: date) -> dict:
    integration, division = metrics.integration, metrics.division
    lo, hi = aware(first), aware(add_months(last, 1))
    if key == "revenue":
        qs = InvoiceLineItem.objects.filter(
            invoice__integration=integration, invoice__issued_date__gte=lo, invoice__issued_date__lt=hi
        ).exclude(invoice__invoice_status="draft").exclude(invoice__client__is_internal=True)
        if division:
            qs = qs.filter(division=division)
        return _by_month(qs, "invoice__issued_date", Sum("net_total"))
    if key in ("visits_completed", "touch_ups"):
        qs = Visit.objects.filter(integration=integration, start_at__gte=lo, start_at__lt=hi).exclude(client__is_internal=True)
        qs = qs.filter(is_complete=True) if key == "visits_completed" else qs.filter(is_touch_up=True, is_cancelled=False)
        if division:
            qs = qs.filter(division=division)
        return _by_month(qs, "start_at", Count("id"))
    if key == "cancelled_visits_value":
        qs = CancellationRecord.objects.filter(
            integration=integration, cancellation_type=CANCELLATION_VISIT, task_date__gte=first, task_date__lt=add_months(last, 1)
        ).exclude(client__is_internal=True)
        if division:
            qs = qs.filter(division=division)
        return _by_month(qs, "task_date", Sum("value"))
    if key == "new_clients":
        out = defaultdict(Decimal)
        division_ids = None
        if division:
            division_ids = set(
                Visit.objects.filter(integration=integration, division=division).values_list("client_id", flat=True).distinct()
            )
        for client_id, first_day in metrics.first_visit_dates().items():
            if first <= first_day < add_months(last, 1) and (division_ids is None or client_id in division_ids):
                out[month_start(first_day)] += 1
        return out
    if key == "labour_hours":
        qs = HubWorkHours.objects.filter(date__gte=first, date__lt=add_months(last, 1))
        return _by_month(qs, "date", Sum("hours_worked"))
    if key == "complaints":
        qs = metrics._hub_clients(HubComplaint.objects.filter(event_date__gte=first, event_date__lt=add_months(last, 1)))
        return _by_month(qs, "event_date", Count("id"))
    if key == "google_new_reviews":
        qs = HubReview.objects.filter(source=HubReview.SOURCE_GOOGLE, submitted_date__gte=first, submitted_date__lt=add_months(last, 1))
        return _by_month(qs, "submitted_date", Count("id"))
    if key == "feedback_score":
        qs = metrics._hub_clients(
            HubReview.objects.exclude(source=HubReview.SOURCE_GOOGLE).filter(
                submitted_date__gte=first, submitted_date__lt=add_months(last, 1), rating__isnull=False
            )
        )
        return {m: v for m, v in _by_month(qs, "submitted_date", Avg("rating")).items()}
    if key in ("mrr", "active_recurring_clients"):
        qs = MrrSnapshot.objects.filter(plan__integration=integration, month__gte=first, month__lte=last)
        if division:
            qs = qs.filter(plan__division=division)
        out = defaultdict(Decimal)
        clients = defaultdict(set)
        for month, status, value, plan_id, client_id in qs.values_list("month", "status", "monthly_value", "plan_id", "plan__client_id"):
            if key == "mrr" and status not in PAUSED_STATUSES:
                out[month] += value
            clients[month].add(client_id or f"p{plan_id}")
        if key == "active_recurring_clients":
            return {month: Decimal(len(ids)) for month, ids in clients.items()}
        return out
    return {}


def _bucket(month: date, grain: str):
    if grain == "year":
        return (month.year,), str(month.year)
    if grain == "quarter":
        q = (month.month - 1) // 3 + 1
        return (month.year, q), f"Q{q} {month.year}"
    return (month.year, month.month), month.strftime("%b %Y")


def series(metrics: Metrics, key: str, range_key: str = "12m", grain: str | None = None, start=None, end=None) -> dict:
    grain = grain or default_grain(range_key)
    first, last = resolve_range(metrics, range_key, start, end)
    months = months_between(first, last)
    meta = KPIS.get(key, {"label": key, "kind": "count", "note": None})

    if key not in TREND_METRICS or meta.get("note"):
        return {"metric": key, "label": meta["label"], "kind": meta["kind"], "available": False, "note": meta.get("note"), "points": []}

    if key == "avg_revenue_per_visit":
        revenue = monthly_series(metrics, "revenue", first, last)
        visits = monthly_series(metrics, "visits_completed", first, last)
    else:
        data = monthly_series(metrics, key, first, last)

    source = KPIS.get(key, {}).get("source") or (
        "cancellations" if key == "cancelled_visits_value" else ("revenue" if key in ("revenue", "avg_revenue_per_visit") else "visits")
    )
    data_start = metrics.data_starts().get(source)
    data_start = month_start(data_start) if data_start else None

    buckets, order = {}, []
    for month in months:
        bucket_key, label = _bucket(month, grain)
        if bucket_key not in buckets:
            buckets[bucket_key] = {"label": label, "start": month.isoformat(), "months": []}
            order.append(bucket_key)
        buckets[bucket_key]["months"].append(month)

    points = []
    for bucket_key in order:
        bucket = buckets[bucket_key]
        in_range = [m for m in bucket["months"] if data_start and m >= data_start]
        if not in_range:
            points.append({"label": bucket["label"], "start": bucket["start"], "value": None})
            continue
        if key == "avg_revenue_per_visit":
            rev = sum((revenue.get(m, Decimal(0)) for m in in_range), Decimal(0))
            vis = sum((visits.get(m, Decimal(0)) for m in in_range), Decimal(0))
            value = (rev / vis).quantize(Decimal("0.01")) if vis else None
        elif key in POINT_IN_TIME:
            value = data.get(in_range[-1], Decimal(0))
        elif key in AVERAGES:
            values = [data[m] for m in in_range if m in data]
            value = (sum(values, Decimal(0)) / len(values)).quantize(Decimal("0.01")) if values else None
        else:
            value = sum((data.get(m, Decimal(0)) for m in in_range), Decimal(0))
        points.append({"label": bucket["label"], "start": bucket["start"], "value": value})

    return {
        "metric": key,
        "label": meta["label"],
        "kind": meta["kind"],
        "available": True,
        "grain": grain,
        "range": range_key,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "points": points,
    }
