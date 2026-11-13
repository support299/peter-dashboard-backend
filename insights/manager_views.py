"""Manager Overview for Cassidy (Peter, Nov 2026): how the business is doing, what needs attention,
staffing and time off, and history. Built from numbers the dashboard already tracks.

Peter's definitions (Nov 10, 2026): bookings won = accepted quotes that aren't recurring; visits to
rebook = past visits never completed; overdue complaint = open more than 1 business day; revenue target =
same month last year + 20%, with forecast and still to book; no billable hours. Still to come: the
capacity forecast, shown once checked against a real week. The capacity check is staff-only until then.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone
from django.views.decorators.http import require_GET

from analytics.hub_models import HubEmployee, HubLeaveRequest
from insights import periods
from insights.attention import REBOOK_DAYS, overdue_complaints, visits_to_rebook
from insights.capacity import capacity_days, forecast, week_totals
from insights.targets import month_forecast
from insights.views import _cards, _context, _no_integration, _period_payload, error, respond

HUB_TIME_OFF = "https://hub.cleanonthego.com/admin/calendar"
FORECAST_DAYS = 28  # the staffing view always looks four weeks ahead

# Card groups follow Peter's mockup: six headline numbers, then Sales / Recurring revenue / Scheduling / Client experience.
HEADLINE = ["leads", "bookings_won", "new_recurring_clients", "mrr_net_new", "visits_completed", "visits_planned", "invoiced_revenue"]
SALES = ["avg_booking_price", "open_leads"]
RECURRING = ["mrr_new", "mrr_expansion", "mrr_contraction", "mrr_churned", "cancelled_recurring_clients"]
SCHEDULING = ["cancelled_visits", "cancelled_recurring_jobs", "late_arrivals", "absence_days"]
CLIENT_EXPERIENCE = ["complaints", "touch_ups", "google_five_star_new", "feedback_score", "feedback_responses"]
WAITING = []

# History: (key, label, grains). Ending MRR only makes sense by month (it's a month-end figure).
HISTORY = [
    ("invoiced_revenue", "Invoiced revenue", ("month", "week")),
    ("revenue_target", "Target", ("month",)),
    ("leads", "Leads", ("month", "week")),
    ("bookings_won", "Bookings won", ("month", "week")),
    ("mrr", "Ending MRR", ("month",)),
    ("new_recurring_clients", "Recurring clients gained", ("month", "week")),
    ("cancelled_recurring_clients", "Recurring clients lost", ("month", "week")),
    ("complaint_rate", "Complaints per 100 visits", ("month", "week")),
    ("touch_up_rate", "Touch-ups per 100 visits", ("month", "week")),
    ("cancelled_visits", "Cancelled visits", ("month", "week")),
    ("late_arrivals", "Late arrivals", ("month", "week")),
    ("feedback_score", "Client feedback (out of 5)", ("month", "week")),
    ("feedback_responses", "Feedback responses", ("month", "week")),
]


def last_completed_week(today: date) -> date:
    return periods.week_start(today) - timedelta(days=7)


def _leave_row(request) -> dict:
    return {
        "id": request.external_id,
        "employee": request.employee_name,
        "role": request.employee_role or (request.employee.role if request.employee else ""),
        "type": request.leave_type or request.leave_type_raw,
        "status": request.status,
        "start": request.start_date,
        "end": request.end_date,
        "days": request.weekday_count,
        "affected_visits": request.affected_visits or [],
        "review_url": f"{HUB_TIME_OFF}?request={request.submission_id or request.external_id}",
    }


def staffing() -> dict:
    staff = HubEmployee.objects.filter(status="active")
    today = timezone.localdate()
    ahead = today + timedelta(days=FORECAST_DAYS)
    leave = HubLeaveRequest.objects.select_related("employee").filter(end_date__gte=today).order_by("start_date")
    pending = [_leave_row(r) for r in leave.filter(status="pending")]
    approved = [_leave_row(r) for r in leave.filter(status="approved", start_date__lte=ahead)]
    is_contractor = lambda row: (row["role"] or "").lower() == "contractor"  # noqa: E731
    return {
        "employees": staff.filter(role="employee").count(),
        "contractors": staff.filter(role="contractor").count(),
        "pending_time_off": [row for row in pending if not is_contractor(row)],
        "approved_time_off": [row for row in approved if not is_contractor(row)],
        # Scheduling only: contractors aren't employees, so their time off is listed on its own.
        "contractor_unavailable": [row for row in pending + approved if is_contractor(row)],
        "hub_time_off_url": HUB_TIME_OFF,
        "window": {"from": today, "to": ahead},
    }


def attention(metrics) -> dict:
    from insights.bookings import to_confirm

    rebook, older = visits_to_rebook(metrics)
    confirm = to_confirm(metrics.integration)
    return {
        "first_visits_to_confirm": len(confirm),
        "visits_to_rebook": rebook.count(),
        "visits_to_rebook_older": older,
        "rebook_days": REBOOK_DAYS,
        "overdue_complaints": overdue_complaints(metrics).count(),
    }


@require_GET
def manager(request):
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    today = timezone.localdate()
    return respond(
        {
            **_period_payload(period, comps),
            "last_completed_week": last_completed_week(today),
            "headline": _cards(metrics, HEADLINE, period, comps),
            "sales": _cards(metrics, SALES, period, comps),
            "recurring": _cards(metrics, RECURRING, period, comps),
            "scheduling": _cards(metrics, SCHEDULING, period, comps),
            "client_experience": _cards(metrics, CLIENT_EXPERIENCE, period, comps),
            "staffing": staffing(),
            "attention": attention(metrics),
            "target": month_forecast(metrics),
            # Always the next 4 weeks from today, whatever period the business overview shows.
            "capacity": forecast(metrics.integration, timezone.localdate(), metrics.division),
            "waiting_for_peter": WAITING,
        }
    )


def history_periods(grain: str, today: date) -> list:
    """The last 12 completed months or weeks, oldest first."""
    if grain == "week":
        start = last_completed_week(today)
        return [periods.build("week", start - timedelta(days=7 * i)) for i in range(11, -1, -1)]
    first = periods.month_start(today)
    return [periods.build("month", periods.add_months(first, -i)) for i in range(12, 0, -1)]


@require_GET
def manager_history(request):
    metrics, _, _ = _context(request)
    if not metrics:
        return _no_integration()
    grain = "week" if request.GET.get("grain") == "week" else "month"
    spans = history_periods(grain, timezone.localdate())
    series = []
    for key, label, grains in HISTORY:
        if grain not in grains:
            continue
        # Before a source's first day (complaints from Aug 20, 2026, late arrivals from Sep 2…) a 0 would
        # read as a perfect month: blank instead, same rule as the cards' comparisons.
        series.append({"key": key, "label": label, "points": [metrics.compared(key, span) for span in spans]})
    return respond(
        {
            "grain": grain,
            # Short labels so twelve fit under a small chart ("Nov", or "Nov 1" for weeks); full names for the hover.
            "labels": [f"{span.start:%b}" if grain == "month" else f"{span.start:%b} {span.start.day}" for span in spans],
            "titles": [span.label for span in spans],
            "starts": [span.start for span in spans],
            "series": series,
        }
    )


@require_GET
def capacity_check(request):
    """Staff-only: what the forecast reads from Jobber, day by day, for checking a real week with Peter."""
    metrics, _, _ = _context(request)
    if not metrics:
        return _no_integration()
    try:
        start = date.fromisoformat(request.GET.get("from") or "") if request.GET.get("from") else periods.week_start(timezone.localdate())
    except ValueError:
        return error("Invalid from date.")
    weeks = []
    for i in range(4):
        days = capacity_days(metrics.integration, start + timedelta(days=7 * i), start + timedelta(days=7 * i + 6), metrics.division)
        weeks.append({"start": start + timedelta(days=7 * i), "days": days, "totals": week_totals(days)})
    return respond({"weeks": weeks})
