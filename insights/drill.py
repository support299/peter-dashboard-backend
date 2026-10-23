"""Drill-down lists: the records behind a Master Dashboard number."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Max, Min, Q, Sum
from django.utils import timezone

from analytics.hub_models import HubComplaint, HubServiceCancellation
from insights.metrics import Metrics
from insights.models import PAUSED_STATUSES, STATUS_ACTIVE, STATUS_CANCELLED, RecurringPlan
from insights.periods import Period, aware
from operations.models import (
    CANCELLATION_JOB,
    CANCELLATION_REASON_CHOICES,
    CANCELLATION_REASON_GROUPS,
    CANCELLATION_VISIT,
    DIVISION_CHOICES,
    REVENUE_CATEGORY_CHOICES,
    Client,
    Invoice,
    Visit,
)

LIMIT = 1000
DIVISIONS = dict(DIVISION_CHOICES)
CATEGORIES = dict(REVENUE_CATEGORY_CHOICES)
REASONS = dict(CANCELLATION_REASON_CHOICES)


def col(key, label, kind="text"):
    return {"key": key, "label": label, "kind": kind}


def _local(dt):
    return timezone.localtime(dt).date() if dt else None


def _client_name(client):
    if not client:
        return "—"
    return client.name or client.company_name or " ".join(p for p in [client.first_name, client.last_name] if p) or "—"


def _result(title, columns, rows, total_rows=None, **extra):
    return {"title": title, "columns": columns, "rows": rows[:LIMIT], "total_rows": total_rows if total_rows is not None else len(rows), **extra}


def drill(metrics: Metrics, key: str, period: Period, *, category: str = "") -> dict:
    handler = HANDLERS.get(key)
    if not handler:
        return _result("Not available", [], [], note="This number has no list behind it yet.")
    return handler(metrics, period, category=category)


# ——— revenue ———
def _revenue(metrics: Metrics, period: Period, category=""):
    lines = metrics._lines(period)
    title = "Revenue"
    if category:
        lines = lines.filter(revenue_category=category)
        title = f"Revenue · {CATEGORIES.get(category, category)}"
    rows = (
        lines.values(
            "invoice_id",
            "invoice__invoice_number",
            "invoice__issued_date",
            "invoice__client__name",
            "invoice__client__company_name",
            "invoice__invoice_status",
            "invoice__jobber_web_uri",
        )
        .annotate(net=Sum("net_total"), gross=Sum("total"), division=Max("division"))
        .order_by("-invoice__issued_date")
    )
    out = [
        {
            "id": row["invoice_id"],
            "kind": "invoices",
            "number": row["invoice__invoice_number"],
            "date": _local(row["invoice__issued_date"]),
            "client": row["invoice__client__name"] or row["invoice__client__company_name"] or "—",
            "division": DIVISIONS.get(row["division"], row["division"] or "—"),
            "status": (row["invoice__invoice_status"] or "").replace("_", " ").title(),
            "amount": row["net"],
            "link": row["invoice__jobber_web_uri"],
        }
        for row in rows
    ]
    total = sum((row["amount"] or Decimal(0) for row in out), Decimal(0))
    return _result(
        title,
        [col("number", "Invoice"), col("date", "Date", "date"), col("client", "Client"), col("division", "Division"), col("status", "Status"), col("amount", "Net revenue", "currency")],
        out,
        total=total,
    )


def _receivables(metrics: Metrics, period: Period, overdue=False, **_):
    qs = metrics._receivables().select_related("client")
    if overdue:
        qs = qs.filter(due_date__lt=aware(timezone.localdate()))
    rows = [
        {
            "id": inv.id,
            "kind": "invoices",
            "number": inv.invoice_number,
            "client": _client_name(inv.client),
            "issued": _local(inv.issued_date),
            "due": _local(inv.due_date),
            "status": (inv.invoice_status or "").replace("_", " ").title(),
            "balance": inv.balance,
            "link": inv.jobber_web_uri,
        }
        for inv in qs.order_by("due_date")
    ]
    return _result(
        "Overdue receivables" if overdue else "Accounts receivable",
        [col("number", "Invoice"), col("client", "Client"), col("issued", "Issued", "date"), col("due", "Due", "date"), col("status", "Status"), col("balance", "Balance", "currency")],
        rows,
        total=sum((r["balance"] or Decimal(0) for r in rows), Decimal(0)),
    )


def _top_clients(metrics: Metrics, period: Period, **_):
    revenue = metrics.value("revenue", period) or Decimal(0)
    rows = []
    for index, row in enumerate(metrics.top_clients(period, 25), start=1):
        rows.append(
            {
                "rank": index,
                "client": row["client"],
                "invoices": row["invoices"],
                "revenue": row["revenue"],
                "share": (row["revenue"] / revenue * 100).quantize(Decimal("0.1")) if revenue else None,
            }
        )
    return _result(
        "Top clients by revenue",
        [col("rank", "#", "count"), col("client", "Client"), col("invoices", "Invoices", "count"), col("revenue", "Revenue", "currency"), col("share", "% of revenue", "percent")],
        rows,
    )


# ——— visits ———
def _visit_rows(qs):
    return [
        {
            "id": v.id,
            "kind": "visits",
            "date": _local(v.start_at),
            "client": _client_name(v.client),
            "title": v.title,
            "division": DIVISIONS.get(v.division, v.division or "—"),
            "team_leader": v.team_leader.full_name if v.team_leader else "",
            "amount": v.price_per_visit,
        }
        for v in qs.select_related("client", "team_leader").order_by("-start_at")[:LIMIT]
    ]


VISIT_COLUMNS = [col("date", "Date", "date"), col("client", "Client"), col("title", "Visit"), col("division", "Division"), col("team_leader", "Team leader"), col("amount", "Value", "currency")]


def _visits_completed(metrics, period, **_):
    qs = metrics._visits(period).filter(is_complete=True)
    return _result("Completed visits", VISIT_COLUMNS, _visit_rows(qs), total_rows=qs.count())


def _touch_ups(metrics, period, **_):
    qs = metrics._visits(period).filter(is_touch_up=True, is_cancelled=False)
    return _result("Touch-up / redo visits", VISIT_COLUMNS, _visit_rows(qs), total_rows=qs.count())


# ——— clients ———
def _client_rows(metrics: Metrics, ids):
    stats = (
        Visit.objects.filter(integration=metrics.integration, client_id__in=ids, is_cancelled=False)
        .values("client_id")
        .annotate(
            last=Max("start_at", filter=Q(is_complete=True)),
            next=Min("start_at", filter=Q(is_complete=False, start_at__gte=timezone.now())),
        )
    )
    by_client = {row["client_id"]: row for row in stats}
    first = metrics.first_visit_dates()
    rows = []
    for client in Client.objects.filter(id__in=ids).order_by("name"):
        info = by_client.get(client.id, {})
        rows.append(
            {
                "id": client.id,
                "kind": "clients",
                "client": _client_name(client),
                "division": DIVISIONS.get(client.division, client.division or "—"),
                "first_visit": first.get(client.id),
                "last_visit": _local(info.get("last")),
                "next_visit": _local(info.get("next")),
            }
        )
    return rows


CLIENT_COLUMNS = [col("client", "Client"), col("division", "Division"), col("first_visit", "First visit", "date"), col("last_visit", "Last completed visit", "date"), col("next_visit", "Next visit", "date")]


def _client_set(name, title):
    def handler(metrics, period, **_):
        ids = metrics.client_flows(period)[name]
        return _result(title, CLIENT_COLUMNS, _client_rows(metrics, ids))

    return handler


# ——— recurring plans ———
def plan_row(plan: RecurringPlan) -> dict:
    return {
        "id": plan.id,
        "client": plan.name,
        "client_id": plan.client_id,
        "division": DIVISIONS.get(plan.division, plan.division or "—"),
        "service": plan.service,
        "frequency": plan.frequency,
        "visit_value": plan.visit_value,
        "monthly_value": plan.effective_monthly_value,
        "pricing_source": plan.pricing_source,
        "pricing_label": plan.get_pricing_source_display(),
        "detected_monthly_value": plan.monthly_value,
        "manual_monthly_value": plan.manual_monthly_value,
        "status": plan.status,
        "detected_status": plan.detected_status,
        "status_override": plan.status_override,
        "pause_reason": plan.pause_reason,
        "pause_start": plan.pause_start,
        "expected_return": plan.expected_return,
        "manual_note": plan.manual_note,
        "start_date": plan.start_date,
        "end_date": plan.end_date,
        "link": plan.job.jobber_web_uri if plan.job_id else "",
    }


PLAN_COLUMNS = [
    col("client", "Client"),
    col("service", "Service"),
    col("frequency", "Frequency"),
    col("visit_value", "Price", "currency"),
    col("pricing_label", "Priced from"),
    col("monthly_value", "MRR", "currency"),
    col("status", "Status", "status"),
    col("start_date", "Start", "date"),
    col("end_date", "End", "date"),
]


def _effective(qs, statuses):
    q = Q(status_override__in=statuses) | Q(status_override="", detected_status__in=statuses)
    return qs.filter(q)


def _plans(title, statuses):
    def handler(metrics, period, **_):
        qs = _effective(metrics.plans().select_related("job"), statuses).order_by("name")
        return _result(title, PLAN_COLUMNS, [plan_row(p) for p in qs], editable="plans")

    return handler


def _new_recurring_jobs(metrics, period, **_):
    qs = metrics.plans().select_related("job").filter(start_date__gte=period.start, start_date__lte=period.end).order_by("start_date")
    return _result("New recurring jobs", PLAN_COLUMNS, [plan_row(p) for p in qs], editable="plans")


def _cancelled_recurring_jobs(metrics, period, **_):
    qs = _effective(metrics.plans().select_related("job"), [STATUS_CANCELLED]).filter(
        end_date__gte=period.start, end_date__lte=period.end
    )
    return _result("Cancelled recurring jobs", PLAN_COLUMNS, [plan_row(p) for p in qs.order_by("end_date")], editable="plans")


MOVEMENT_COLUMNS = [
    col("client", "Client"),
    col("month", "Month", "month"),
    col("service", "Frequency"),
    col("division", "Division"),
    col("before", "MRR before", "currency"),
    col("after", "MRR after", "currency"),
    col("change", "Change", "currency"),
    col("reason", "Cancellation reason"),
]


def _movement(title, kinds):
    def handler(metrics, period, **_):
        result = metrics.mrr(period)
        rows = [dict(row) for row in result.rows if row["type"] in kinds] if result.available else []
        _attach_reasons(metrics, rows, period)
        for row in rows:
            row["division"] = DIVISIONS.get(row["division"], row["division"] or "—")
        total = sum((row["change"] for row in rows), Decimal(0))
        return _result(title, MOVEMENT_COLUMNS, rows, total=total)

    return handler


def _norm(name):
    return " ".join((name or "").lower().split())


def _attach_reasons(metrics: Metrics, rows, period: Period):
    """
    Reason a recurring client left: the Hub's Service Cancellation Form first
    (matched by Jobber client id, else by name, within ~2 months of the period),
    then the reason set on the Jobber 'Cancelled Job' record.
    """
    churned = [row for row in rows if row.get("client_id") and row["type"] == "churned"]
    reasons = {}
    if churned:
        client_ids = [row["client_id"] for row in churned]
        for client_id, reason, notes in (
            metrics.cancellations(period, CANCELLATION_JOB)
            .filter(client_id__in=client_ids)
            .values_list("client_id", "reason", "notes")
        ):
            reasons[client_id] = REASONS.get(reason) or notes or ""
        clients = {c.id: c for c in Client.objects.filter(id__in=client_ids).only("id", "external_id", "name")}
        by_jobber = {c.external_id: c.id for c in clients.values()}
        by_name = {_norm(c.name): c.id for c in clients.values() if c.name}
        forms = HubServiceCancellation.objects.filter(
            submitted_date__gte=period.start - timedelta(days=60), submitted_date__lte=period.end + timedelta(days=31)
        ).order_by("submitted_at")
        for form in forms:
            client_id = by_jobber.get(form.client_jobber_id) or by_name.get(_norm(form.client_name))
            if client_id:
                detail = ", ".join(form.sub_reasons or [])
                reasons[client_id] = f"{form.reason} ({detail})" if detail else form.reason
    for row in rows:
        row["reason"] = reasons.get(row.get("client_id"), "")


def _lost_recurring_clients(metrics, period, **_):
    result = metrics.mrr(period)
    rows = [dict(row) for row in result.rows if row["type"] == "churned"] if result.available else []
    _attach_reasons(metrics, rows, period)
    return _result("Recurring clients lost", MOVEMENT_COLUMNS, rows)


# ——— cancellations ———
def cancellation_row(record) -> dict:
    return {
        "id": record.id,
        "type": "Visit" if record.cancellation_type == CANCELLATION_VISIT else "Recurring job",
        "date": record.task_date,
        "client": record.client_name or _client_name(record.client),
        "division": DIVISIONS.get(record.division, record.division or "—"),
        "value": record.value,
        "reason": record.reason,
        "reason_label": REASONS.get(record.reason, ""),
        "reason_group": CANCELLATION_REASON_GROUPS.get(record.reason, ""),
        "reason_notes": record.reason_notes,
        "notes": record.notes,
    }


CANCEL_COLUMNS = [
    col("date", "Date", "date"),
    col("type", "Type"),
    col("client", "Client"),
    col("division", "Division"),
    col("value", "Value", "currency"),
    col("reason_label", "Reason"),
    col("reason_notes", "Notes"),
]


def _cancellations(title, kind, internal=False):
    def handler(metrics, period, **_):
        qs = metrics.cancellations(period, kind).select_related("client")
        if internal:
            qs = qs.filter(reason__in=[r for r, g in CANCELLATION_REASON_GROUPS.items() if g == "internal"])
        rows = [cancellation_row(r) for r in qs.order_by("-task_date", "-id")]
        return _result(title, CANCEL_COLUMNS, rows, total=sum((r["value"] or Decimal(0) for r in rows), Decimal(0)), editable="cancellations")

    return handler


# ——— Hub: feedback, Google, complaints, hours, absences, service cancellations ———
def _names(people):
    return ", ".join(p.get("name") or "" for p in people or [] if p.get("name"))


REVIEW_COLUMNS = [
    col("date", "Date", "date"),
    col("source", "Source"),
    col("client", "Client"),
    col("rating", "Rating", "count"),
    col("cleaners", "Cleaners"),
    col("comment", "Comment"),
]
REVIEW_SOURCES = {"how_are_we_doing": "How are we doing?", "review_your_recent_experience": "Recent experience", "google": "Google"}


def _review_rows(qs):
    return [
        {
            "id": r.id,
            "date": r.submitted_date,
            "source": REVIEW_SOURCES.get(r.source, r.source),
            "client": r.client_name,
            "rating": r.rating,
            "cleaners": _names(r.cleaners),
            "comment": " · ".join(t for t in [r.comment, r.improvement_area] if t),
        }
        for r in qs.order_by("-submitted_at")
    ]


def _feedback(metrics, period, **_):
    return _result("Hub feedback", REVIEW_COLUMNS, _review_rows(metrics.feedback(period)))


def _google_new(metrics, period, **_):
    qs = metrics.google().filter(submitted_date__gte=period.start, submitted_date__lte=period.end)
    return _result("New Google reviews", REVIEW_COLUMNS, _review_rows(qs))


def _google_all(metrics, period, **_):
    return _result("Google reviews", REVIEW_COLUMNS, _review_rows(metrics.google().filter(submitted_date__lte=period.end)))


COMPLAINT_COLUMNS = [
    col("date", "Date", "date"),
    col("client", "Client"),
    col("type", "Type"),
    col("responsible", "Responsible"),
    col("team", "Team"),
    col("status", "Status"),
    col("resolved", "Resolved", "date"),
    col("days", "Days to resolve", "count"),
    col("what_happened", "What happened"),
    col("how_fixed", "How it was fixed"),
]


def _complaint_rows(qs):
    rows = []
    for c in qs.order_by("-event_date", "-id"):
        rows.append(
            {
                "id": c.id,
                "date": c.event_date,
                "client": c.client_name,
                "type": c.complaint_type,
                "responsible": _names(c.responsible),
                "team": _names(c.team),
                "status": (c.status or "").replace("_", " ").capitalize(),
                "resolved": c.resolved_date,
                "days": (c.resolved_date - c.event_date).days if c.resolved_date and c.event_date else None,
                "what_happened": c.what_happened,
                "how_fixed": c.how_fixed,
            }
        )
    return rows


def _complaints(metrics, period, **_):
    return _result("Complaints", COMPLAINT_COLUMNS, _complaint_rows(metrics.complaints(period)))


def _complaints_open(metrics, period, **_):
    qs = metrics.complaints(period).exclude(status=HubComplaint.STATUS_RESOLVED)
    return _result("Complaints not resolved yet", COMPLAINT_COLUMNS, _complaint_rows(qs))


def _complaints_resolved(metrics, period, **_):
    qs = metrics._hub_clients(HubComplaint.objects.filter(resolved_date__gte=period.start, resolved_date__lte=period.end))
    return _result("Complaints resolved", COMPLAINT_COLUMNS, _complaint_rows(qs))


def _labour_hours(metrics, period, **_):
    rows = [
        {
            "date": h.date,
            "employee": h.employee_name,
            "hours": h.hours_worked,
            "regular": h.regular_hours,
            "drive": h.drive_time_hours,
            "overtime": h.overtime_hours,
            "source": h.source.capitalize(),
        }
        for h in metrics.hours(period).order_by("-date", "employee_name")
    ]
    return _result(
        "Labour hours (Jobber timesheets via the Hub)",
        [
            col("date", "Date", "date"),
            col("employee", "Employee"),
            col("hours", "Hours worked", "count"),
            col("regular", "Regular", "count"),
            col("drive", "Drive time", "count"),
            col("overtime", "Overtime", "count"),
            col("source", "Source"),
        ],
        rows,
        total=None,
    )


def _absences(metrics, period, **_):
    rows = [
        {"employee": a.employee_name, "start": a.start_date, "end": a.end_date, "days": a.weekday_count, "status": a.status}
        for a in metrics.absences(period).order_by("-start_date")
    ]
    return _result(
        "Absences",
        [col("employee", "Employee"), col("start", "From", "date"), col("end", "To", "date"), col("days", "Weekdays", "count"), col("status", "Status")],
        rows,
    )


def _service_cancellations(metrics, period, **_):
    rows = [
        {
            "date": f.submitted_date,
            "client": f.client_name,
            "frequency": f.frequency,
            "plan": f.plan_type,
            "reason": f.reason,
            "details": ", ".join(f.sub_reasons or []),
            "comments": f.comments,
        }
        for f in metrics.service_cancellations(period).order_by("-submitted_at")
    ]
    return _result(
        "Service cancellation forms",
        [
            col("date", "Date", "date"),
            col("client", "Client"),
            col("frequency", "Frequency"),
            col("plan", "Plan"),
            col("reason", "Reason"),
            col("details", "Details"),
            col("comments", "Comments"),
        ],
        rows,
    )


HANDLERS = {
    "revenue": _revenue,
    "receivables": _receivables,
    "receivables_overdue": lambda m, p, **kw: _receivables(m, p, overdue=True),
    "top_clients": _top_clients,
    "visits_completed": _visits_completed,
    "touch_ups": _touch_ups,
    "active_clients": _client_set("end", "Active clients at end of period"),
    "active_clients_start": _client_set("start", "Active clients at start of period"),
    "new_clients": _client_set("new", "New clients"),
    "returning_clients": _client_set("returning", "Returning clients"),
    "lost_clients": _client_set("lost", "Lost clients"),
    "recurring_clients": _plans("Active and paused recurring plans", [STATUS_ACTIVE, *PAUSED_STATUSES]),
    "mrr_plans": _plans("Active recurring plans", [STATUS_ACTIVE]),
    "paused_clients": _plans("Paused recurring plans", list(PAUSED_STATUSES)),
    "new_recurring_jobs": _new_recurring_jobs,
    "cancelled_recurring_jobs": _cancelled_recurring_jobs,
    "lost_recurring_clients": _lost_recurring_clients,
    "mrr_new": _movement("New MRR", {"new"}),
    "mrr_expansion": _movement("Expansion MRR", {"expansion"}),
    "mrr_reactivated": _movement("Reactivated MRR", {"reactivated"}),
    "mrr_contraction": _movement("Contraction MRR", {"contraction"}),
    "mrr_churned": _movement("Churned MRR", {"churned"}),
    "mrr_paused": _movement("Paused / resumed MRR", {"paused", "resumed"}),
    "mrr_untracked": _movement("MRR changes before tracking started", {"untracked"}),
    "cancelled_visits": _cancellations("Cancelled visits", CANCELLATION_VISIT),
    "cancelled_visits_internal": _cancellations("Cancellations caused internally", CANCELLATION_VISIT, internal=True),
    "cancelled_jobs": _cancellations("Cancelled recurring jobs (Jobber tasks)", CANCELLATION_JOB),
    "feedback": _feedback,
    "google_reviews": _google_all,
    "google_new_reviews": _google_new,
    "complaints": _complaints,
    "complaints_open": _complaints_open,
    "complaints_resolved": _complaints_resolved,
    "labour_hours": _labour_hours,
    "absences": _absences,
    "service_cancellations": _service_cancellations,
}
