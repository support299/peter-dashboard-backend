"""
Needs Attention on the Manager Overview (Peter, Nov 10 2026). Always "as of today", whatever period is picked.

Visits to rebook     = past visits (before today) that were never completed and aren't cancelled, e.g. a cleaner
                       called in sick. A visit leaves the list once it's completed, moved to a new date or
                       deleted in Jobber. Only the last REBOOK_DAYS days are listed, so years-old leftovers
                       don't bury yesterday's; older ones are counted separately.
Overdue complaints   = complaints open for more than 1 business day (Mon–Fri). They stay until marked resolved
                       in the Hub.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

from analytics.hub_models import HubComplaint
from insights.periods import aware

REBOOK_DAYS = 30
OVERDUE_BUSINESS_DAYS = 1


def previous_business_day(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def overdue_before(today: date) -> date:
    """Complaints submitted before this day have been open more than 1 business day."""
    day = today
    for _ in range(OVERDUE_BUSINESS_DAYS):
        day = previous_business_day(day)
    return day


def visits_to_rebook(metrics, today: date | None = None):
    """(listed visits, older count)."""
    from operations.models import Visit

    today = today or timezone.localdate()
    qs = Visit.objects.filter(integration=metrics.integration, start_at__lt=aware(today), is_complete=False, is_cancelled=False).exclude(
        client__is_internal=True
    )
    if metrics.division:
        qs = qs.filter(division=metrics.division)
    since = aware(today - timedelta(days=REBOOK_DAYS))
    return qs.filter(start_at__gte=since), qs.filter(start_at__lt=since).count()


def overdue_complaints(metrics, today: date | None = None):
    today = today or timezone.localdate()
    qs = HubComplaint.objects.exclude(status__iexact=HubComplaint.STATUS_RESOLVED).filter(submitted_date__lt=overdue_before(today))
    return metrics._hub_clients(qs)
