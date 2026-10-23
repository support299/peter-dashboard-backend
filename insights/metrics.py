"""Master Dashboard KPIs.

Every KPI is defined once in KPIS (label, format, which direction is good, which
drill-down list it opens). Metrics.value(key, period) computes it for any period;
None means the data does not exist for that period and the UI shows
"Unavailable" instead of an estimate.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Min, Q, Sum
from django.utils import timezone

from analytics.hub_models import HubComplaint, HubLeaveRequest, HubReview, HubServiceCancellation, HubWorkHours
from insights.models import RecurringPlan, STATUS_CANCELLED
from insights.mrr import movements
from insights.periods import Period, aware
from operations.models import (
    CANCELLATION_JOB,
    CANCELLATION_REASON_GROUPS,
    CANCELLATION_VISIT,
    CancellationRecord,
    Client,
    Invoice,
    InvoiceLineItem,
    Visit,
)

ACTIVE_LOOKBACK_DAYS = 90  # completed visit within the last 90 days …
ACTIVE_LOOKAHEAD_DAYS = 60  # … or a booking coming up

FINANCIALS_NOTE = "Needs the Financials section (expenses and payroll), coming in the next stage."
LATE_ARRIVALS_NOTE = "The Hub only sends a running total of late arrivals, not dated records, so it can't be split by period yet."
HOURS_DIVISION_NOTE = "Hours are not split by division, so this is shown for all divisions only."
PAYMENTS_NOTE = "Needs Jobber payment dates in the sync, coming in a later stage."
LEADS_NOTE = "Needs Jobber requests and UMB leads in the sync, coming in a later stage."


def kpi(label, kind="currency", better="up", drill=None, note=None, point_in_time=False, source=None):
    return {"label": label, "kind": kind, "better": better, "drill": drill, "note": note, "point_in_time": point_in_time, "source": source}


KPIS = {
    # Revenue & profitability
    "revenue": kpi("Revenue", drill="revenue"),
    "invoiced_revenue": kpi("Invoiced revenue", drill="revenue"),
    "discounts": kpi("Discounts", better="down", drill="revenue"),
    "completed_visit_value": kpi("Completed visit value", drill="visits_completed"),
    "payments_received": kpi("Payments received", note=PAYMENTS_NOTE),
    "gross_profit": kpi("Gross profit", note=FINANCIALS_NOTE),
    "gross_margin": kpi("Gross margin %", kind="percent", note=FINANCIALS_NOTE),
    "net_profit": kpi("Net profit", note=FINANCIALS_NOTE),
    "net_margin": kpi("Net profit margin %", kind="percent", note=FINANCIALS_NOTE),
    "cogs": kpi("COGS", better="down", note=FINANCIALS_NOTE),
    "payroll": kpi("Payroll", better="down", note=FINANCIALS_NOTE),
    "payroll_pct": kpi("Payroll % of revenue", kind="percent", better="down", note=FINANCIALS_NOTE),
    "operating_expenses": kpi("Operating expenses", better="down", note=FINANCIALS_NOTE),
    # Visits
    "visits_completed": kpi("Completed visits", kind="count", drill="visits_completed"),
    "avg_revenue_per_visit": kpi("Avg revenue per visit"),
    # Clients
    "active_clients": kpi("Active clients", kind="count", drill="active_clients", point_in_time=True),
    "active_clients_start": kpi("Starting active clients", kind="count", drill="active_clients_start", point_in_time=True),
    "new_clients": kpi("New clients", kind="count", drill="new_clients"),
    "returning_clients": kpi("Returning clients", kind="count", drill="returning_clients"),
    "lost_clients": kpi("Lost clients", kind="count", better="down", drill="lost_clients"),
    "net_client_growth": kpi("Net client growth", kind="count", drill=None),
    # Recurring
    "active_recurring_clients": kpi("Active recurring clients", kind="count", drill="recurring_clients", point_in_time=True),
    "paused_clients": kpi("Paused clients", kind="count", better="down", drill="paused_clients", point_in_time=True),
    "recurring_retention": kpi("Recurring client retention %", kind="percent", drill="lost_recurring_clients"),
    "new_recurring_jobs": kpi("New recurring jobs", kind="count", drill="new_recurring_jobs"),
    "cancelled_recurring_jobs": kpi("Cancelled recurring jobs", kind="count", better="down", drill="cancelled_recurring_jobs"),
    "net_recurring_jobs": kpi("Net new recurring jobs", kind="count"),
    # MRR
    "mrr": kpi("Current MRR", drill="mrr_plans", point_in_time=True),
    "mrr_starting": kpi("Starting MRR", point_in_time=True),
    "mrr_new": kpi("New MRR", drill="mrr_new"),
    "mrr_expansion": kpi("Expansion MRR", drill="mrr_expansion"),
    "mrr_reactivated": kpi("Reactivated MRR", drill="mrr_reactivated"),
    "mrr_contraction": kpi("Contraction MRR", better="down", drill="mrr_contraction"),
    "mrr_churned": kpi("Churned MRR", better="down", drill="mrr_churned"),
    "mrr_paused_net": kpi("Paused / resumed MRR", drill="mrr_paused"),
    "mrr_untracked": kpi("Other changes (before tracking)", drill="mrr_untracked"),
    "mrr_net_new": kpi("Net new MRR"),
    "mrr_paused": kpi("Paused MRR", better="down", drill="paused_clients", point_in_time=True),
    # Cancellations
    "cancelled_visits": kpi("Cancelled visits", kind="count", better="down", drill="cancelled_visits"),
    "cancelled_visits_value": kpi("Value of cancelled visits", better="down", drill="cancelled_visits"),
    "cancelled_jobs": kpi("Cancelled recurring jobs (tasks)", kind="count", better="down", drill="cancelled_jobs"),
    "cancelled_jobs_value": kpi("Monthly value of cancelled jobs", better="down", drill="cancelled_jobs"),
    "cancellations_internal": kpi("Cancellations caused internally", kind="count", better="down", drill="cancelled_visits_internal"),
    # Quality
    "touch_ups": kpi("Touch-up / redo visits", kind="count", better="down", drill="touch_ups"),
    "touch_up_rate": kpi("Redo rate per 100 visits", kind="rate", better="down", drill="touch_ups"),
    "feedback_score": kpi("Hub feedback score (out of 5)", kind="decimal", drill="feedback", source="feedback"),
    "feedback_responses": kpi("Hub feedback responses", kind="count", drill="feedback", source="feedback"),
    "feedback_participation": kpi("Feedback participation %", kind="percent", drill="feedback", source="feedback"),
    "complaints": kpi("Complaints", kind="count", better="down", drill="complaints", source="complaints"),
    "complaint_rate": kpi("Complaint rate per 100 visits", kind="rate", better="down", drill="complaints", source="complaints"),
    "complaints_open": kpi("Complaints not resolved yet", kind="count", better="down", drill="complaints_open", source="complaints"),
    "complaint_resolution_days": kpi("Avg days to resolve a complaint", kind="decimal", better="down", drill="complaints_resolved", source="complaints"),
    "google_rating": kpi("Google average rating", kind="decimal", drill="google_reviews", point_in_time=True, source="google"),
    "google_reviews": kpi("Google reviews (total)", kind="count", drill="google_reviews", point_in_time=True, source="google"),
    "google_new_reviews": kpi("New Google reviews", kind="count", drill="google_new_reviews", source="google"),
    "google_five_star_new": kpi("New 5-star Google reviews", kind="count", drill="google_new_reviews", source="google"),
    # Operations & labour (hours: Jobber timesheets via the Hub)
    "labour_hours": kpi("Labour hours", kind="decimal", better="down", drill="labour_hours", source="hours"),
    "overtime_hours": kpi("Overtime hours", kind="decimal", better="down", drill="labour_hours", source="hours"),
    "drive_time_hours": kpi("Drive time hours", kind="decimal", better="down", drill="labour_hours", source="hours"),
    "revenue_per_labour_hour": kpi("Revenue per labour hour", source="hours"),
    "absence_days": kpi("Absence days", kind="count", better="down", drill="absences"),
    "late_arrivals": kpi("Late arrivals", kind="count", better="down", note=LATE_ARRIVALS_NOTE),
    "service_cancellation_forms": kpi("Service cancellation forms", kind="count", better="down", drill="service_cancellations", source="service_cancellations"),
    # Concentration & receivables
    "top5_share": kpi("Top 5 clients % of revenue", kind="percent", better="down", drill="top_clients"),
    "top10_share": kpi("Top 10 clients % of revenue", kind="percent", better="down", drill="top_clients"),
    "ar_balance": kpi("Accounts receivable", better="down", drill="receivables", point_in_time=True),
    "ar_overdue": kpi("Overdue receivables", better="down", drill="receivables_overdue", point_in_time=True),
    # Sales (later stage)
    "leads": kpi("Leads", kind="count", note=LEADS_NOTE),
    "lead_conversion": kpi("Lead → client conversion %", kind="percent", note=LEADS_NOTE),
}

SCORECARD = [
    "revenue",
    "gross_profit",
    "gross_margin",
    "net_profit",
    "net_margin",
    "mrr",
    "active_clients",
    "active_recurring_clients",
    "visits_completed",
    "avg_revenue_per_visit",
    "payroll_pct",
    "recurring_retention",
]

# KPIs scanned by the "What changed?" panel.
CHANGE_WATCH = [
    "revenue",
    "visits_completed",
    "avg_revenue_per_visit",
    "active_clients",
    "new_clients",
    "lost_clients",
    "active_recurring_clients",
    "recurring_retention",
    "new_recurring_jobs",
    "cancelled_recurring_jobs",
    "mrr",
    "mrr_new",
    "mrr_churned",
    "mrr_net_new",
    "cancelled_visits",
    "cancelled_visits_value",
    "touch_ups",
    "top5_share",
    "complaints",
    "feedback_score",
    "google_new_reviews",
    "labour_hours",
    "revenue_per_labour_hour",
]


def _day(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _d(value) -> Decimal:
    return Decimal(value or 0)


def _pct(part, whole):
    if whole in (None, 0) or part is None:
        return None
    return (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1"))


class HubMetrics:
    """Mixin: Hub reviews, complaints, service cancellations, work hours and absences."""

    def _hub_clients(self, qs):
        """Division filter for Hub records, through the Jobber client they're matched to."""
        if not self.division:
            return qs
        ids = Client.objects.filter(integration=self.integration, division=self.division).values("external_id")
        return qs.filter(client_jobber_id__in=ids)

    # ——— Hub feedback ———
    def feedback(self, period: Period):
        qs = HubReview.objects.exclude(source=HubReview.SOURCE_GOOGLE).filter(
            submitted_date__gte=period.start, submitted_date__lte=period.end
        )
        return self._hub_clients(qs)

    def _feedback_score(self, period):
        if not self._has("feedback", period):
            return None
        avg = self.feedback(period).filter(rating__isnull=False).aggregate(a=Avg("rating"))["a"]
        return Decimal(str(avg)).quantize(Decimal("0.01")) if avg is not None else None

    def _feedback_responses(self, period):
        return self.feedback(period).count() if self._has("feedback", period) else None

    def _feedback_participation(self, period):
        responses, visits = self.value("feedback_responses", period), self.value("visits_completed", period)
        if responses is None or not visits:
            return None
        return _pct(responses, visits)

    # ——— Google (no division: reviews are for the whole company) ———
    def google(self):
        return HubReview.objects.filter(source=HubReview.SOURCE_GOOGLE)

    def _google_rating(self, period):
        if not self._has("google", period):
            return None
        avg = self.google().filter(submitted_date__lte=period.end, rating__isnull=False).aggregate(a=Avg("rating"))["a"]
        return Decimal(str(avg)).quantize(Decimal("0.01")) if avg is not None else None

    def _google_reviews(self, period):
        return self.google().filter(submitted_date__lte=period.end).count() if self._has("google", period) else None

    def _google_new_reviews(self, period):
        if not self._has("google", period):
            return None
        return self.google().filter(submitted_date__gte=period.start, submitted_date__lte=period.end).count()

    def _google_five_star_new(self, period):
        if not self._has("google", period):
            return None
        return self.google().filter(submitted_date__gte=period.start, submitted_date__lte=period.end, rating=5).count()

    # ——— complaints ———
    def complaints(self, period: Period):
        return self._hub_clients(HubComplaint.objects.filter(event_date__gte=period.start, event_date__lte=period.end))

    def _complaints(self, period):
        return self.complaints(period).count() if self._has("complaints", period) else None

    def _complaint_rate(self, period):
        count, visits = self.value("complaints", period), self.value("visits_completed", period)
        if count is None or not visits:
            return None
        return (Decimal(count) / Decimal(visits) * 100).quantize(Decimal("0.01"))

    def _complaints_open(self, period):
        if not self._has("complaints", period):
            return None
        return self.complaints(period).exclude(status=HubComplaint.STATUS_RESOLVED).count()

    def _complaint_resolution_days(self, period):
        if not self._has("complaints", period):
            return None
        rows = (
            self._hub_clients(HubComplaint.objects.filter(resolved_date__gte=period.start, resolved_date__lte=period.end))
            .exclude(event_date__isnull=True)
            .values_list("event_date", "resolved_date")
        )
        days = [max((resolved - start).days, 0) for start, resolved in rows]
        return (Decimal(sum(days)) / len(days)).quantize(Decimal("0.1")) if days else None

    def complaint_types(self, period: Period) -> list:
        rows = self.complaints(period).values("complaint_type").annotate(n=Count("id")).order_by("-n")
        return [{"label": row["complaint_type"] or "Not specified", "count": row["n"]} for row in rows]

    # ——— service cancellation form ———
    def service_cancellations(self, period: Period):
        qs = HubServiceCancellation.objects.filter(submitted_date__gte=period.start, submitted_date__lte=period.end)
        return self._hub_clients(qs)

    def _service_cancellation_forms(self, period):
        return self.service_cancellations(period).count() if self._has("service_cancellations", period) else None

    def service_cancellation_reasons(self, period: Period) -> list:
        reasons = defaultdict(lambda: {"count": 0, "details": defaultdict(int)})
        for reason, subs in self.service_cancellations(period).values_list("reason", "sub_reasons"):
            row = reasons[reason or "Not specified"]
            row["count"] += 1
            for sub in subs or []:
                row["details"][sub] += 1
        return [
            {
                "label": label,
                "count": row["count"],
                "details": [{"label": k, "count": v} for k, v in sorted(row["details"].items(), key=lambda kv: -kv[1])],
            }
            for label, row in sorted(reasons.items(), key=lambda kv: -kv[1]["count"])
        ]

    # ——— hours ———
    def hours(self, period: Period):
        return HubWorkHours.objects.filter(date__gte=period.start, date__lte=period.end)

    def _hours_sum(self, period, field):
        if not self._has("hours", period):
            return None
        return _d(self.hours(period).aggregate(s=Sum(field))["s"])

    def _labour_hours(self, period):
        return self._hours_sum(period, "hours_worked")

    def _overtime_hours(self, period):
        return self._hours_sum(period, "overtime_hours")

    def _drive_time_hours(self, period):
        return self._hours_sum(period, "drive_time_hours")

    def _revenue_per_labour_hour(self, period):
        start = self.data_starts().get("hours")
        # Only when hours cover the whole period and the whole company; otherwise the ratio would mislead.
        if self.division or not start or period.start < start:
            return None
        revenue, hours = self.value("revenue", period), self.value("labour_hours", period)
        if revenue is None or not hours:
            return None
        return (revenue / hours).quantize(Decimal("0.01"))

    # ——— absences (Hub leave requests) ———
    def absences(self, period: Period):
        return (
            HubLeaveRequest.objects.filter(leave_type__iexact="absent", start_date__lte=period.end)
            .filter(Q(end_date__gte=period.start) | Q(end_date__isnull=True, start_date__gte=period.start))
            .exclude(status__iregex=r"^(rejected|declined|cancel+ed)$")
        )

    def _absence_days(self, period):
        total = 0
        for start, end, weekdays in self.absences(period).values_list("start_date", "end_date", "weekday_count"):
            total += weekdays or max(((end or start) - start).days + 1, 1)
        return total

    # ——— people ———
    def employee_table(self, period: Period) -> list:
        """Per employee: hours, overtime, Hub reviews they were on, complaints they were responsible for."""
        people = defaultdict(lambda: {"hours": Decimal(0), "overtime": Decimal(0), "reviews": 0, "rating_sum": 0, "rated": 0, "complaints": 0})

        def key(person):
            return person.get("jobber_user_id") or person.get("id") or person.get("name")

        names = {}
        for row in self.hours(period).values("jobber_user_id", "employee_external_id", "employee_name", "hours_worked", "overtime_hours"):
            k = row["jobber_user_id"] or row["employee_external_id"]
            names[k] = row["employee_name"]
            people[k]["hours"] += row["hours_worked"] or 0
            people[k]["overtime"] += row["overtime_hours"] or 0
        for cleaners, rating in self.feedback(period).values_list("cleaners", "rating"):
            for person in cleaners or []:
                k = key(person)
                names.setdefault(k, person.get("name") or "—")
                people[k]["reviews"] += 1
                if rating:
                    people[k]["rating_sum"] += rating
                    people[k]["rated"] += 1
        for responsible in self.complaints(period).values_list("responsible", flat=True):
            for person in responsible or []:
                k = key(person)
                names.setdefault(k, person.get("name") or "—")
                people[k]["complaints"] += 1
        rows = []
        for k, row in people.items():
            rows.append(
                {
                    "employee": names.get(k) or "—",
                    "hours": row["hours"],
                    "overtime": row["overtime"],
                    "reviews": row["reviews"],
                    "avg_rating": (Decimal(row["rating_sum"]) / row["rated"]).quantize(Decimal("0.01")) if row["rated"] else None,
                    "complaints": row["complaints"],
                }
            )
        return sorted(rows, key=lambda r: (-r["hours"], r["employee"]))


class Metrics(HubMetrics):
    """Computes KPIs for any period, with per-request caching."""

    def __init__(self, integration, division: str = ""):
        self.integration = integration
        self.division = division or ""
        self._cache = {}
        self._starts = None

    # ——— data availability ———
    def data_starts(self) -> dict:
        if self._starts is None:
            inv = Invoice.objects.filter(integration=self.integration).exclude(invoice_status="draft")
            first_invoice = inv.aggregate(m=Min("issued_date"))["m"]
            first_cancel = CancellationRecord.objects.filter(integration=self.integration).aggregate(m=Min("task_date"))["m"]
            first_visit = Visit.objects.filter(integration=self.integration).aggregate(m=Min("start_at"))["m"]
            reviews = HubReview.objects.aggregate(
                feedback=Min("submitted_date", filter=~Q(source=HubReview.SOURCE_GOOGLE)),
                google=Min("submitted_date", filter=Q(source=HubReview.SOURCE_GOOGLE)),
            )
            self._starts = {
                "revenue": timezone.localtime(first_invoice).date() if first_invoice else None,
                "cancellations": first_cancel,
                "visits": timezone.localtime(first_visit).date() if first_visit else None,
                "feedback": reviews["feedback"],
                "google": reviews["google"],
                "complaints": HubComplaint.objects.aggregate(m=Min("event_date"))["m"],
                "hours": HubWorkHours.objects.aggregate(m=Min("date"))["m"],
                "service_cancellations": HubServiceCancellation.objects.aggregate(m=Min("submitted_date"))["m"],
            }
        return self._starts

    def _has(self, source: str, period: Period) -> bool:
        start = self.data_starts().get(source)
        return bool(start) and period.end >= start

    # ——— public ———
    def value(self, key: str, period: Period):
        cache_key = (key, period.start, period.end)
        if cache_key not in self._cache:
            method = getattr(self, f"_{key}", None)
            self._cache[cache_key] = method(period) if method else None
        return self._cache[cache_key]

    def card(self, key: str, period: Period, comparisons: dict) -> dict:
        meta = KPIS[key]
        current = self.value(key, period)
        payload = {
            "key": key,
            **meta,
            "available": current is not None,
            "current": current,
            "previous": None,
            "last_year": None,
        }
        # Receivables are a live balance with no history, so they have no comparison.
        if key not in {"ar_balance", "ar_overdue"}:
            for name, other in comparisons.items():
                payload[name] = self.value(key, other)
        start = self.data_starts().get(meta["source"]) if meta["source"] else None
        if current is None and not meta["note"]:
            payload["note"] = self._unavailable_note(key)
        elif current is not None and start and period.start < start and not meta["point_in_time"]:
            # Data began part-way through the period: say so rather than imply a full period.
            payload["note"] = f"Only includes data from {_day(start)}."
        if meta["source"] == "hours" and self.division and key != "revenue_per_labour_hour":
            payload["note"] = HOURS_DIVISION_NOTE
        return payload

    def _unavailable_note(self, key):
        if key.startswith("cancel"):
            start = self.data_starts().get("cancellations")
            return f"Cancellation records start on {_day(start)}." if start else "No cancellation records yet."
        source = KPIS[key].get("source")
        if source:
            start = self.data_starts().get(source)
            if key == "revenue_per_labour_hour" and self.division:
                return HOURS_DIVISION_NOTE
            if key == "revenue_per_labour_hour" and start:
                return f"Needs hours for the whole period; hours start on {_day(start)}."
            return f"Data starts on {_day(start)}." if start else "No data received yet."
        return "No data for this period."

    # ——— revenue ———
    def _lines(self, period: Period):
        qs = InvoiceLineItem.objects.filter(
            invoice__integration=self.integration,
            invoice__issued_date__gte=period.start_dt,
            invoice__issued_date__lt=period.end_dt,
        ).exclude(invoice__invoice_status="draft").exclude(invoice__client__is_internal=True)
        if self.division:
            qs = qs.filter(division=self.division)
        return qs

    def _revenue(self, period):
        if not self._has("revenue", period):
            return None
        return _d(self._lines(period).aggregate(s=Sum("net_total"))["s"])

    def _invoiced_revenue(self, period):
        if not self._has("revenue", period):
            return None
        return _d(self._lines(period).aggregate(s=Sum("total"))["s"])

    def _discounts(self, period):
        invoiced, net = self.value("invoiced_revenue", period), self.value("revenue", period)
        return None if invoiced is None else invoiced - net

    def revenue_by(self, field: str, period: Period) -> dict:
        if not self._has("revenue", period):
            return {}
        rows = self._lines(period).values(field).annotate(s=Sum("net_total"))
        return {row[field] or "": _d(row["s"]) for row in rows}

    def top_clients(self, period: Period, limit=10):
        rows = (
            self._lines(period)
            .values("invoice__client_id", "invoice__client__name", "invoice__client__company_name")
            .annotate(s=Sum("net_total"), invoices=Count("invoice", distinct=True))
            .order_by("-s")[:limit]
        )
        return [
            {
                "client_id": row["invoice__client_id"],
                "client": row["invoice__client__name"] or row["invoice__client__company_name"] or "—",
                "revenue": _d(row["s"]),
                "invoices": row["invoices"],
            }
            for row in rows
        ]

    def _top_share(self, period, n):
        revenue = self.value("revenue", period)
        if not revenue:
            return None
        top = sum((row["revenue"] for row in self.top_clients(period, n)), Decimal(0))
        return _pct(top, revenue)

    def _top5_share(self, period):
        return self._top_share(period, 5)

    def _top10_share(self, period):
        return self._top_share(period, 10)

    def _receivables(self):
        qs = (
            Invoice.objects.filter(integration=self.integration, balance__gt=0)
            .exclude(invoice_status__in=["draft", "bad_debt"])
            .exclude(client__is_internal=True)
        )
        if self.division:
            qs = qs.filter(division=self.division)
        return qs

    def _ar_balance(self, period):
        return _d(self._receivables().aggregate(s=Sum("balance"))["s"])

    def _ar_overdue(self, period):
        today_start = aware(timezone.localdate())
        return _d(self._receivables().filter(due_date__lt=today_start).aggregate(s=Sum("balance"))["s"])

    # ——— visits ———
    def _visits(self, period: Period):
        qs = Visit.objects.filter(integration=self.integration, start_at__gte=period.start_dt, start_at__lt=period.end_dt).exclude(
            client__is_internal=True
        )
        if self.division:
            qs = qs.filter(division=self.division)
        return qs

    def _visits_completed(self, period):
        if not self._has("visits", period):
            return None
        return self._visits(period).filter(is_complete=True).count()

    def _completed_visit_value(self, period):
        if not self._has("visits", period):
            return None
        return _d(self._visits(period).filter(is_complete=True).aggregate(s=Sum("price_per_visit"))["s"])

    def _avg_revenue_per_visit(self, period):
        revenue, visits = self.value("revenue", period), self.value("visits_completed", period)
        if revenue is None or not visits:
            return None
        return (revenue / visits).quantize(Decimal("0.01"))

    def _touch_ups(self, period):
        if not self._has("visits", period):
            return None
        return self._visits(period).filter(is_touch_up=True, is_cancelled=False).count()

    def _touch_up_rate(self, period):
        touch, visits = self.value("touch_ups", period), self.value("visits_completed", period)
        if touch is None or not visits:
            return None
        return (Decimal(touch) / Decimal(visits) * 100).quantize(Decimal("0.01"))

    # ——— clients ———
    def active_client_ids(self, day: date) -> set:
        key = ("active_ids", day)
        if key not in self._cache:
            lo = aware(day - timedelta(days=ACTIVE_LOOKBACK_DAYS - 1))
            mid = aware(day + timedelta(days=1))
            hi = mid + timedelta(days=ACTIVE_LOOKAHEAD_DAYS)
            qs = Visit.objects.filter(
                integration=self.integration, client__isnull=False, client__is_internal=False, is_cancelled=False
            ).filter(
                Q(is_complete=True, start_at__gte=lo, start_at__lt=mid) | Q(start_at__gte=mid, start_at__lt=hi)
            )
            if self.division:
                qs = qs.filter(division=self.division)
            self._cache[key] = set(qs.values_list("client_id", flat=True).distinct())
        return self._cache[key]

    def first_visit_dates(self) -> dict:
        key = ("first_visits",)
        if key not in self._cache:
            rows = (
                Visit.objects.filter(integration=self.integration, client__isnull=False, client__is_internal=False, is_complete=True)
                .values("client_id")
                .annotate(first=Min("start_at"))
            )
            self._cache[key] = {row["client_id"]: timezone.localtime(row["first"]).date() for row in rows}
        return self._cache[key]

    def client_flows(self, period: Period) -> dict:
        key = ("flows", period.start, period.end)
        if key not in self._cache:
            start_ids = self.active_client_ids(period.start - timedelta(days=1))
            end_ids = self.active_client_ids(period.end)
            first = self.first_visit_dates()
            gained = end_ids - start_ids
            new = {cid for cid in gained if first.get(cid) and period.start <= first[cid] <= period.end}
            self._cache[key] = {
                "start": start_ids,
                "end": end_ids,
                "new": new,
                "returning": gained - new,
                "lost": start_ids - end_ids,
            }
        return self._cache[key]

    def _clients_ok(self, period):
        return self._has("visits", period)

    def _active_clients(self, period):
        return len(self.client_flows(period)["end"]) if self._clients_ok(period) else None

    def _active_clients_start(self, period):
        return len(self.client_flows(period)["start"]) if self._clients_ok(period) else None

    def _new_clients(self, period):
        return len(self.client_flows(period)["new"]) if self._clients_ok(period) else None

    def _returning_clients(self, period):
        return len(self.client_flows(period)["returning"]) if self._clients_ok(period) else None

    def _lost_clients(self, period):
        return len(self.client_flows(period)["lost"]) if self._clients_ok(period) else None

    def _net_client_growth(self, period):
        if not self._clients_ok(period):
            return None
        flows = self.client_flows(period)
        return len(flows["new"]) - len(flows["lost"])

    # ——— recurring / MRR ———
    def mrr(self, period: Period):
        key = ("mrr_movements", period.start, period.end)
        if key not in self._cache:
            self._cache[key] = movements(self.integration, period.start, period.end, self.division)
        return self._cache[key]

    def _mrr_value(self, period, attr):
        result = self.mrr(period)
        if not result.available:
            return None
        return getattr(result, attr)

    def _mrr(self, period):
        return self._mrr_value(period, "ending")

    def _mrr_starting(self, period):
        return self._mrr_value(period, "starting")

    def _mrr_net_new(self, period):
        return self._mrr_value(period, "net_new")

    def _mrr_paused(self, period):
        return self._mrr_value(period, "paused_ending")

    def _movement(self, period, kind):
        result = self.mrr(period)
        if not result.available:
            return None
        if kind in ("expansion", "contraction") and period.end < result.expansion_tracked_from:
            return None
        return result.totals[kind]

    def _mrr_new(self, period):
        return self._movement(period, "new")

    def _mrr_expansion(self, period):
        return self._movement(period, "expansion")

    def _mrr_reactivated(self, period):
        return self._movement(period, "reactivated")

    def _mrr_contraction(self, period):
        return self._movement(period, "contraction")

    def _mrr_churned(self, period):
        return self._movement(period, "churned")

    def _mrr_untracked(self, period):
        return self._movement(period, "untracked")

    def _mrr_paused_net(self, period):
        result = self.mrr(period)
        if not result.available:
            return None
        return result.totals["paused"] + result.totals["resumed"]

    def _active_recurring_clients(self, period):
        result = self.mrr(period)
        return len(result.recurring_end) if result.available else None

    def _paused_clients(self, period):
        result = self.mrr(period)
        return len(result.paused_end) if result.available else None

    def _recurring_retention(self, period):
        result = self.mrr(period)
        if not result.available or not result.recurring_start:
            return None
        kept = len(result.recurring_start & result.recurring_end)
        return _pct(kept, len(result.recurring_start))

    def plans(self):
        qs = RecurringPlan.objects.filter(integration=self.integration).exclude(client__is_internal=True)
        if self.division:
            qs = qs.filter(division=self.division)
        return qs

    def _new_recurring_jobs(self, period):
        if not self._has("visits", period):
            return None
        return self.plans().filter(start_date__gte=period.start, start_date__lte=period.end).count()

    def _cancelled_recurring_jobs(self, period):
        if not self._has("visits", period):
            return None
        qs = self.plans().filter(end_date__gte=period.start, end_date__lte=period.end)
        return qs.filter(Q(status_override=STATUS_CANCELLED) | Q(status_override="", detected_status=STATUS_CANCELLED)).count()

    def _net_recurring_jobs(self, period):
        new, lost = self.value("new_recurring_jobs", period), self.value("cancelled_recurring_jobs", period)
        return None if new is None or lost is None else new - lost

    # ——— cancellations ———
    def cancellations(self, period: Period, kind=None):
        qs = CancellationRecord.objects.filter(
            integration=self.integration, task_date__gte=period.start, task_date__lte=period.end
        ).exclude(client__is_internal=True)
        if kind:
            qs = qs.filter(cancellation_type=kind)
        if self.division:
            qs = qs.filter(division=self.division)
        return qs

    def _cancelled_visits(self, period):
        return self.cancellations(period, CANCELLATION_VISIT).count() if self._has("cancellations", period) else None

    def _cancelled_visits_value(self, period):
        if not self._has("cancellations", period):
            return None
        return _d(self.cancellations(period, CANCELLATION_VISIT).aggregate(s=Sum("value"))["s"])

    def _cancelled_jobs(self, period):
        return self.cancellations(period, CANCELLATION_JOB).count() if self._has("cancellations", period) else None

    def _cancelled_jobs_value(self, period):
        if not self._has("cancellations", period):
            return None
        return _d(self.cancellations(period, CANCELLATION_JOB).aggregate(s=Sum("value"))["s"])

    def _cancellations_internal(self, period):
        if not self._has("cancellations", period):
            return None
        internal = [reason for reason, group in CANCELLATION_REASON_GROUPS.items() if group == "internal"]
        return self.cancellations(period, CANCELLATION_VISIT).filter(reason__in=internal).count()

    def reason_breakdown(self, period: Period) -> list:
        rows = defaultdict(lambda: {"count": 0, "value": Decimal(0)})
        for reason, value in self.cancellations(period, CANCELLATION_VISIT).values_list("reason", "value"):
            rows[reason or ""]["count"] += 1
            rows[reason or ""]["value"] += _d(value)
        return rows


def what_changed(metrics: Metrics, period: Period, other: Period, limit=8) -> list:
    """Largest meaningful moves between two periods, flagged good or bad."""
    out = []
    for key in CHANGE_WATCH:
        meta = KPIS[key]
        cur, prev = metrics.value(key, period), metrics.value(key, other)
        if cur is None or prev is None:
            continue
        cur, prev = Decimal(cur), Decimal(prev)
        diff = cur - prev
        if diff == 0:
            continue
        if meta["kind"] == "percent":
            if abs(diff) < Decimal("1.5"):
                continue
            score = abs(diff) * 2
            pct = None
        else:
            floor = Decimal(100) if meta["kind"] == "currency" else Decimal(2)
            if abs(diff) < floor:
                continue
            pct = (diff / abs(prev) * 100).quantize(Decimal("0.1")) if prev else None
            if pct is not None and abs(pct) < 5:
                continue
            score = abs(pct) if pct is not None else Decimal(100)
        up = diff > 0
        out.append(
            {
                "key": key,
                "label": meta["label"],
                "kind": meta["kind"],
                "current": cur,
                "comparison": prev,
                "diff": diff,
                "pct": pct,
                "direction": "up" if up else "down",
                "good": up == (meta["better"] == "up"),
                "drill": meta["drill"],
                "score": score,
            }
        )
    out.sort(key=lambda row: row["score"], reverse=True)
    for row in out:
        row.pop("score")
    return out[:limit]
