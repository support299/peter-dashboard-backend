"""Merge the old MRR tab (MrrBooking) with the Jobber recurring plans.

Jobber is the source of MRR. Each old-tab booking is linked to the Jobber plan it
describes (by client name, or by hand), so its extra details (sales person,
cancellation reason, notes) show with the plan and nothing is counted twice.
Bookings with no Jobber plan are listed for review; they are not added to MRR.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import defaultdict

from analytics.mrr_models import MrrBooking
from insights.models import STATUS_CANCELLED, RecurringPlan


def norm(text) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def plan_names(plan) -> set:
    client = plan.client
    names = {plan.name}
    if client:
        names |= {client.name, client.company_name, f"{client.first_name} {client.last_name}"}
    return {norm(n) for n in names if n and norm(n)}


def _index(integration):
    index = defaultdict(list)
    plans = list(RecurringPlan.objects.filter(integration=integration).select_related("client"))
    for plan in plans:
        for name in plan_names(plan):
            index[name].append(plan)
    return index, plans


def best_plan(booking, candidates):
    active = booking.status == MrrBooking.STATUS_ACTIVE

    def score(plan):
        same_state = (plan.status != STATUS_CANCELLED) == active
        gap = abs((plan.start_date - booking.start_date).days) if plan.start_date and booking.start_date else 10**6
        return (not same_state, gap)

    return min({p.id: p for p in candidates}.values(), key=score)


def link_bookings(integration) -> dict:
    """Auto-link bookings by name; links made or rejected by hand are never changed."""
    index, _ = _index(integration)
    changed, linked = [], 0
    for booking in MrrBooking.objects.exclude(plan_match__in=[MrrBooking.MATCH_MANUAL, MrrBooking.MATCH_IGNORED]):
        candidates = index.get(norm(booking.name), [])
        plan = best_plan(booking, candidates) if candidates else None
        match = MrrBooking.MATCH_AUTO if plan else ""
        if booking.plan_id != (plan.id if plan else None) or booking.plan_match != match:
            booking.plan, booking.plan_match = plan, match
            changed.append(booking)
        linked += bool(plan)
    MrrBooking.objects.bulk_update(changed, ["plan", "plan_match"])
    return {"linked": linked, "changed": len(changed)}


def suggestions(booking, plans, limit=3) -> list:
    target = norm(booking.name)
    scored = []
    for plan in plans:
        ratio = max((difflib.SequenceMatcher(None, target, name).ratio() for name in plan_names(plan)), default=0)
        if ratio >= 0.5:
            scored.append((ratio, plan))
    scored.sort(key=lambda item: -item[0])
    return [plan for _, plan in scored[:limit]]
