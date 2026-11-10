"""
Approved bookings (Peter, Nov 2026): accepted Jobber quotes and approved Pricing Calculator bookings,
where the client booked themselves. Used for "Bookings won" and "First visits to confirm".

A booking         = a Jobber quote approved or converted (dated when it changed status), or a calculator
                    booking approved by the client (dated by its last update, the approval). A calculator
                    booking whose person also has an accepted Jobber quote within MATCH_DAYS is the same
                    booking: counted once, as the quote.
Recurring         = the client got a recurring job created from the booking's creation to MATCH_DAYS after
                    approval. Bookings won leaves these out (Peter: accepted bookings that are not recurring).
Scheduled         = a visit (not cancelled) was created for the client on/after the booking: on a new job,
                    or added to a job they already had.
First visits to confirm = bookings of the last CONFIRM_DAYS days that aren't scheduled yet, one-off or recurring.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

MATCH_DAYS = 30
CONFIRM_DAYS = 60
QUOTE_ACCEPTED = {"approved", "converted"}
CALC_ACCEPTED = {"approved"}


@dataclass
class Booking:
    source: str  # "quote" | "calculator"
    id: int
    created: datetime
    approved: datetime
    name: str
    amount: Decimal | None
    link: str = ""
    reference: str = ""
    title: str = ""
    client_ids: set = field(default_factory=set)
    recurring: bool = False
    scheduled: bool = False

    @property
    def kind(self) -> str:
        return "Recurring" if self.recurring else "One-off"


def _name(first, last, company, fallback=""):
    return " ".join(p for p in (first, last) if p).strip() or company or fallback


def approved_bookings(integration, start: datetime, end: datetime) -> list[Booking]:
    """Bookings approved in [start, end), oldest first."""
    from analytics.pricing_models import PricingSubmission
    from insights.sales import is_test_email, norm_email, norm_phone
    from operations.models import Client, Job, Quote, Visit

    clients = Client.objects.filter(integration=integration).values("id", "email", "phone", "is_internal")
    internal = {c["id"] for c in clients if c["is_internal"]}
    by_email, by_phone = defaultdict(set), defaultdict(set)
    for c in clients:
        if c["id"] in internal:
            continue
        if norm_email(c["email"]):
            by_email[norm_email(c["email"])].add(c["id"])
        if norm_phone(c["phone"]):
            by_phone[norm_phone(c["phone"])].add(c["id"])

    bookings = []
    for q in (
        Quote.objects.filter(integration=integration, quote_status__in=QUOTE_ACCEPTED, transitioned_at__gte=start, transitioned_at__lt=end)
        .exclude(client__is_internal=True)
        .select_related("client")
    ):
        client = q.client
        name = _name(client.first_name, client.last_name, client.company_name, client.name) if client else ""
        bookings.append(
            Booking("quote", q.id, q.source_created_at or q.transitioned_at, q.transitioned_at, client.name if client and client.name else name, q.subtotal if q.subtotal is not None else q.total,
                    link=q.jobber_web_uri, reference=f"Quote #{q.quote_number}" if q.quote_number else "Quote", title=q.title, client_ids={q.client_id} if q.client_id else set())
        )
    # The calculator has no "approved at": its last update is the approval (nothing edits it after that).
    for s in PricingSubmission.objects.filter(is_deleted=False, status__in=CALC_ACCEPTED, source_updated_at__gte=start, source_updated_at__lt=end):
        email, phone = norm_email(s.customer_email), norm_phone(s.customer_phone)
        if is_test_email(email) or not (email or phone):
            continue
        ids = by_email.get(email, set()) | by_phone.get(phone, set())
        services = ", ".join(s.service_names or [])
        bookings.append(
            Booking("calculator", s.id, s.source_created_at or s.source_updated_at, s.source_updated_at, _name(s.customer_first_name, s.customer_last_name, s.customer_company, email), s.final_total,
                    link=s.quote_url, reference="Pricing Calculator", title=services, client_ids=ids)
        )

    # The same person booking through the calculator and an accepted Jobber quote is one booking.
    quote_days = defaultdict(list)
    for b in bookings:
        if b.source == "quote":
            for cid in b.client_ids:
                quote_days[cid].append(b.approved)
    window = timedelta(days=MATCH_DAYS)
    bookings = [
        b for b in bookings
        if b.source == "quote" or not any(abs(day - b.approved) <= window for cid in b.client_ids for day in quote_days.get(cid, []))
    ]

    all_ids = {cid for b in bookings for cid in b.client_ids}
    jobs = defaultdict(list)  # client -> [(created, recurring)]
    for cid, created, recurring in Job.objects.filter(integration=integration, client_id__in=all_ids).values_list("client_id", "source_created_at", "is_recurring"):
        if created:
            jobs[cid].append((created, recurring))
    visits = defaultdict(list)  # client -> [visit created]
    for cid, created in Visit.objects.filter(integration=integration, client_id__in=all_ids, is_cancelled=False).values_list("client_id", "source_created_at"):
        if created:
            visits[cid].append(created)
    for b in bookings:
        low = b.created - timedelta(days=1)
        high = b.approved + window
        b.recurring = any(recurring and low <= created <= high for cid in b.client_ids for created, recurring in jobs.get(cid, []))
        b.scheduled = any(created >= low for cid in b.client_ids for created in visits.get(cid, []))
    return sorted(bookings, key=lambda b: b.approved)


def to_confirm(integration, now: datetime | None = None) -> list[Booking]:
    """First visits to confirm: approved in the last CONFIRM_DAYS days, nothing scheduled yet. Newest first."""
    now = now or timezone.now()
    rows = approved_bookings(integration, now - timedelta(days=CONFIRM_DAYS), now + timedelta(days=1))
    return [b for b in reversed(rows) if not b.scheduled]
