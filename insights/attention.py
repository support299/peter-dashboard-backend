"""
Needs Attention on the Manager Overview (Peter, Nov 10 2026). Always "as of today", whatever period is picked.

Visits to rebook     = past visits (before today) that were never completed and aren't cancelled, e.g. a cleaner
                       called in sick. A visit leaves the list once it's completed, moved to a new date or
                       deleted in Jobber. Only the last REBOOK_DAYS days are listed, so years-old leftovers
                       don't bury yesterday's; older ones are counted separately.
Overdue complaints   = complaints open for more than 1 business day (Mon–Fri). They stay until marked resolved
                       in the Hub.
Needs recurring quote (Peter, Nov 26 2026) = a one-off first or deep clean (Peter, Nov 27: only visits whose
                       title, or their job's, says "first clean" or "deep clean"; move-in/out cleans don't
                       count) completed more than 24 hours ago, in the
                       last RECURRING_QUOTE_DAYS days, whose client got no "Recurring Service" / "Service
                       Régulier" quote sent since the visit was booked, and has no recurring job (not archived).
                       It leaves the list once the quote is sent, even late; a declined (archived) quote counts as
                       sent. One row per client (their latest such clean, 24 hours after it).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from django.utils import timezone

from analytics.hub_models import HubComplaint
from insights.periods import aware

REBOOK_DAYS = 30
OVERDUE_BUSINESS_DAYS = 1
RECURRING_QUOTE_DAYS = 30
RECURRING_QUOTE_WAIT = timedelta(hours=24)
RECURRING_QUOTE_TITLE = re.compile(r"recurr|r[ée]guli[eè]r|regular", re.I)
# "First Cleaning", "Deep-clean", and the one French title used ("Premier nettoyage").
QUOTE_AFTER_TITLE = r"first[ -]?clean|deep[ -]?clean|premi[eè]re? nettoyage"


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


def _done_at(visit):
    return visit.completed_at or visit.end_at or visit.start_at


def needs_recurring_quote(metrics, now: datetime | None = None) -> list:
    """Completed one-off first/deep cleans still waiting for a recurring quote, latest first (one per client)."""
    from django.db.models import Q

    from operations.models import Job, Quote, Visit

    now = now or timezone.now()
    since = aware(timezone.localdate(now) - timedelta(days=RECURRING_QUOTE_DAYS))
    qs = (
        Visit.objects.filter(
            integration=metrics.integration, is_complete=True, is_cancelled=False, is_recurring=False,
            client__isnull=False, start_at__gte=since, start_at__lt=now,
        )
        .filter(Q(title__iregex=QUOTE_AFTER_TITLE) | Q(job__title__iregex=QUOTE_AFTER_TITLE))
        .exclude(client__is_internal=True)
        .exclude(is_touch_up=True)  # a redo added to a first-clean job isn't a clean of its own
        .select_related("client", "team_leader", "job")
        .order_by("-start_at")
    )
    if metrics.division:
        qs = qs.filter(division=metrics.division)
    latest, seen = {}, set()
    for visit in qs:
        if visit.client_id in seen:
            continue
        seen.add(visit.client_id)
        # The 24 hours count from the client's last first/deep clean (some take several visits).
        if now - _done_at(visit) > RECURRING_QUOTE_WAIT:
            latest[visit.client_id] = visit
    if not latest:
        return []
    recurring = set(
        Job.objects.filter(integration=metrics.integration, client_id__in=latest, is_recurring=True)
        .exclude(job_status="archived")
        .values_list("client_id", flat=True)
    )
    sent = {}  # client -> when each recurring quote went out
    for client_id, title, status, sent_at, changed, created in Quote.objects.filter(integration=metrics.integration, client_id__in=latest).values_list(
        "client_id", "title", "quote_status", "sent_at", "transitioned_at", "source_created_at"
    ):
        # A draft hasn't gone to the client yet; any other status means it was sent (or declined/archived).
        when = sent_at or (None if status in ("", "draft") else changed or created)
        if when and RECURRING_QUOTE_TITLE.search(title or ""):
            sent.setdefault(client_id, []).append(when)
    rows = []
    for client_id, visit in latest.items():
        if client_id in recurring:
            continue
        booked = visit.source_created_at or _done_at(visit) - timedelta(days=RECURRING_QUOTE_DAYS)
        if any(when >= booked for when in sent.get(client_id, [])):
            continue
        rows.append(visit)
    return rows
