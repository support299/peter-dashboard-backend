"""
Revenue vs. target for the current month (Peter, Nov 10 2026).

Target            = invoiced revenue in the same month last year + 20%.
Forecast          = invoiced so far this month + what we expect to invoice by month-end from confirmed
                    bookings that haven't been invoiced yet.
Still to book     = target − forecast; at or above target: "On track to hit target".

Expected from bookings = this month's visits that are completed, or still to come (today onward), not
cancelled, and not on an invoice yet. Each visit is valued like Completed visit value (accounts invoiced
monthly count their share of the monthly price). Past visits never completed are left out: they're on the
"Visits to rebook" list, and count again once rebooked. A monthly-invoiced account whose invoice for this
month is already out adds nothing more.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from insights import periods
from insights.models import PRICING_INVOICE
from insights.periods import aware


def expected_visits(metrics, today: date | None = None):
    """This month's visits still expected to be invoiced (see the module notes)."""
    from django.db.models import Q

    from operations.models import Invoice

    today = today or timezone.localdate()
    whole = periods.build("month", today).whole()
    visits = metrics._visits(whole).filter(is_cancelled=False, invoice__isnull=True).filter(Q(is_complete=True) | Q(start_at__gte=aware(today)))
    invoiced_jobs = (
        Invoice.objects.filter(integration=metrics.integration, issued_date__gte=whole.start_dt, issued_date__lt=whole.end_dt, jobs__isnull=False)
        .exclude(invoice_status="draft")
        .values("jobs")
    )
    return visits.exclude(job__recurring_plan__pricing_source=PRICING_INVOICE, job_id__in=invoiced_jobs)


def month_forecast(metrics, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    month = periods.build("month", today)  # month to date
    whole = month.whole()
    target = metrics.value("revenue_target", month)
    invoiced = metrics.value("invoiced_revenue", month) or Decimal(0)
    visits = expected_visits(metrics, today)
    expected = metrics.visits_value(visits)
    forecast = invoiced + expected
    still = max(target - forecast, Decimal(0)) if target is not None else None
    return {
        "month": whole.label,
        "target": target,
        "last_year": metrics.value("invoiced_revenue", whole.shifted(months=-12)),
        "invoiced": invoiced,
        "expected": expected,
        "forecast": forecast,
        "still_to_book": still,
        "on_track": target is not None and forecast >= target,
        "progress": float(min(forecast / target, Decimal(1))) if target else None,
        "visits_counted": visits.count(),
        "unpriced_visits": visits.filter(price_per_visit__isnull=True).exclude(job__recurring_plan__pricing_source=PRICING_INVOICE).count(),
    }
