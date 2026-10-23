"""MRR from Jobber recurring jobs.

Nightly (rebuild_mrr):
  * one RecurringPlan per recurring Job: price per visit from the next scheduled
    visits, frequency from the spacing between visits, status from the schedule;
  * price / frequency / status changes are logged as MrrChange rows;
  * MrrSnapshot holds each plan's end-of-month MRR. Months before tracking
    started are rebuilt from visit history; from then on the current month is
    written every night and past tracked months are left untouched, so
    historical MRR is preserved when prices change.

Per period (movements): MRR is compared client by client, month by month, so a
client who swaps one Jobber job for another counts as an expansion or
contraction rather than a cancellation plus a new client.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from insights.models import (
    PAUSED_STATUSES,
    PRICING_INVOICE,
    PRICING_NONE,
    PRICING_VISIT,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    InsightsSetting,
    MrrChange,
    MrrSnapshot,
    RecurringPlan,
)
from insights.periods import add_months, month_end, month_start, months_between
from operations.models import Invoice, Job, Visit

CENTS = Decimal("0.01")
TRACKING_KEY = "mrr_tracking_start"
# A recurring job with no future visit still counts as active this many days after its last visit
# (Jobber schedules that ran out but have not been renewed or closed yet).
ACTIVE_GRACE_DAYS = 35

# (min gap, max gap, label, visits per month) — client formulas: weekly = 52/12, bi-weekly = 26/12, monthly = 1.
FREQUENCY_BUCKETS = [
    (5.5, 8.5, "Weekly", Decimal(52) / Decimal(12)),
    (11.0, 17.0, "Bi-Weekly", Decimal(26) / Decimal(12)),
    (19.0, 23.5, "Every 3 weeks", Decimal(52) / Decimal(36)),
    (26.0, 29.3, "Every 4 weeks", Decimal(13) / Decimal(12)),
    (29.3, 34.0, "Monthly", Decimal(1)),
    (40.0, 46.0, "Every 6 weeks", Decimal(52) / Decimal(72)),
    (55.0, 65.0, "Every 2 months", Decimal("0.5")),
]


def _q(value) -> Decimal:
    return Decimal(value or 0).quantize(CENTS, rounding=ROUND_HALF_UP)


def frequency_from_dates(dates: list[date]):
    """Average spacing between visit days → (label, visits per month), or None."""
    days = sorted(set(dates))
    if len(days) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    gap = statistics.mean(gaps)
    if gap <= 0:
        return None
    for low, high, label, per_month in FREQUENCY_BUCKETS:
        if low <= gap <= high:
            return label, per_month
    per_month = (Decimal("365.25") / Decimal(12) / Decimal(str(gap))).quantize(Decimal("0.0001"))
    if gap < 5.5:
        return f"{round(7 / gap, 1):g}x per week", per_month
    return f"Every {round(gap)} days", per_month


# Invoices are compared with visit prices over this window (or since the job started, if newer).
INVOICE_WINDOW_DAYS = 92
DAYS_PER_MONTH = Decimal("30.4375")
# Visit-based MRR this many times above what was really invoiced is not believable.
OVERSTATED_RATIO = Decimal(3)
# A visit price this close to a whole invoice, with several visits per invoice, means each
# visit carries the monthly / fixed price (Globe Electric: $4,880 per visit, $4,880 per invoice).
SAME_AS_INVOICE = Decimal("0.8")


def _median_price(prices):
    prices = [p for p in prices if p is not None and p > 0]
    return Decimal(statistics.median(prices)) if prices else None


@dataclass
class VisitRow:
    day: date
    price: Decimal | None
    complete: bool


@dataclass
class InvoiceRow:
    day: date
    amount: Decimal


@dataclass
class Detection:
    start_date: date | None
    end_date: date | None
    status: str
    visit_value: Decimal
    frequency: str
    visits_per_month: Decimal
    monthly_value: Decimal
    pricing_source: str = PRICING_VISIT
    rows: list = field(default_factory=list)
    invoices: list = field(default_factory=list)


def _window(end: date, start: date | None):
    lo = end - timedelta(days=INVOICE_WINDOW_DAYS)
    return max(lo, start) if start else lo


def invoiced_monthly(invoices: list, end: date, start: date | None = None):
    """
    What the job was actually invoiced per month over the window ending `end`:
    (average per month, latest invoice amount, invoices per month, invoice count), or None.
    The latest invoice is the current price (Globe Electric: $4,520 in August, $4,880 since September).
    """
    lo = _window(end, start)
    recent = sorted((i for i in invoices if lo < i.day <= end and i.amount and i.amount > 0), key=lambda i: i.day)
    if not recent:
        return None
    amounts = [i.amount for i in recent]
    months = max(Decimal((end - lo).days) / DAYS_PER_MONTH, Decimal(1))
    per_month = Decimal(len(amounts)) / months
    nearest = per_month.to_integral_value()
    if nearest >= 1 and abs(per_month - nearest) <= Decimal("0.35"):
        per_month = nearest  # three invoices in ~3 months = monthly billing
    return _q(sum(amounts, Decimal(0)) / months), amounts[-1], per_month, len(amounts)


def price_plan(visit_price, visits_per_month, invoices, end: date, start: date | None, visits=()):
    """(monthly value, value shown as "price", pricing source) for a plan."""
    visit_price = Decimal(visit_price or 0)
    visit_monthly = _q(visit_price * visits_per_month)
    billed = invoiced_monthly(invoices, end, start)
    if billed:
        average, latest, per_month, invoice_count = billed
        lo = _window(end, start)
        done = sum(1 for v in visits if v.complete and lo < v.day <= end)
        fixed_price_on_visits = visit_price >= latest * SAME_AS_INVOICE and done >= 2 * invoice_count
        if visit_monthly == 0 or fixed_price_on_visits or visit_monthly > average * OVERSTATED_RATIO:
            return _q(latest * per_month), _q(latest), PRICING_INVOICE
    if visit_monthly > 0:
        return visit_monthly, _q(visit_price), PRICING_VISIT
    return Decimal("0.00"), Decimal("0.00"), PRICING_NONE


def _schedule_terms(upcoming: list, past: list):
    """Price per visit and frequency as the schedule stood: the next visits first, else the last ones."""
    price = _median_price([r.price for r in upcoming[:3]]) or _median_price([r.price for r in past[-3:]])
    freq = frequency_from_dates([r.day for r in upcoming[:8]]) or frequency_from_dates([r.day for r in past[-8:]])
    return price, freq


def detect(job: Job, rows: list[VisitRow], today: date, invoices: list | None = None) -> Detection:
    rows = sorted(rows, key=lambda r: r.day)
    upcoming = [r for r in rows if r.day >= today and not r.complete]
    past = [r for r in rows if not (r.day >= today and not r.complete)]
    start = rows[0].day if rows else (timezone.localtime(job.start_at).date() if job.start_at else None)
    last_past = past[-1].day if past else None

    if not rows:
        # No visit ever scheduled: no service, so it never counts (end = start keeps it out of every month).
        status, end = STATUS_CANCELLED, start
    elif upcoming:
        status, end = STATUS_ACTIVE, None
    elif job.job_status != "archived" and last_past and (today - last_past).days <= ACTIVE_GRACE_DAYS:
        status, end = STATUS_ACTIVE, None
    else:
        status = STATUS_CANCELLED
        end = last_past or (timezone.localtime(job.end_at).date() if job.end_at else start)

    price, freq = _schedule_terms(upcoming, past)
    if price is None:
        price = job.total if job.total and job.total > 0 else Decimal("0")
    label, per_month = freq or ("Monthly", Decimal(1))
    invoices = invoices or []
    monthly, shown_price, source = price_plan(price, per_month, invoices, min(today, end or today), start, rows)
    return Detection(
        start_date=start,
        end_date=end,
        status=status,
        visit_value=shown_price,
        frequency=label,
        visits_per_month=per_month.quantize(Decimal("0.0001")),
        monthly_value=monthly,
        pricing_source=source,
        rows=rows,
        invoices=invoices,
    )


def tracking_start() -> date:
    value = InsightsSetting.get(TRACKING_KEY)
    if value:
        return date.fromisoformat(value["date"])
    start = timezone.localdate()
    InsightsSetting.put(TRACKING_KEY, {"date": start.isoformat()})
    return start


def tracking_month() -> date:
    return month_start(tracking_start())


def _visit_rows(integration, job_ids):
    rows = defaultdict(list)
    qs = (
        Visit.objects.filter(integration=integration, job_id__in=job_ids, is_cancelled=False, start_at__isnull=False)
        .values_list("job_id", "start_at", "price_per_visit", "is_complete")
        .iterator(chunk_size=5000)
    )
    for job_id, start_at, price, complete in qs:
        rows[job_id].append(VisitRow(timezone.localtime(start_at).date(), price, bool(complete)))
    return rows


def _invoice_rows(integration, job_ids):
    rows = defaultdict(list)
    qs = (
        Invoice.objects.filter(integration=integration, jobs__id__in=job_ids, issued_date__isnull=False)
        .exclude(invoice_status="draft")
        .values_list("jobs__id", "issued_date", "net_revenue", "subtotal")
        .iterator(chunk_size=5000)
    )
    for job_id, issued, net, subtotal in qs:
        amount = net if net is not None else subtotal
        rows[job_id].append(InvoiceRow(timezone.localtime(issued).date(), Decimal(amount or 0)))
    return rows


def _log_changes(plan: RecurringPlan, det: Detection, today: date, changes: list):
    if plan.pk is None:
        return
    old_monthly = plan.effective_monthly_value
    if plan.visit_value != det.visit_value and abs(plan.visit_value - det.visit_value) >= Decimal("0.50"):
        changes.append(
            MrrChange(
                plan=plan,
                changed_on=today,
                change_type=MrrChange.TYPE_PRICE,
                old_value=str(plan.visit_value),
                new_value=str(det.visit_value),
                monthly_delta=_q(det.monthly_value - old_monthly) if plan.manual_monthly_value is None else Decimal(0),
            )
        )
    if plan.frequency and plan.frequency != det.frequency:
        changes.append(
            MrrChange(
                plan=plan,
                changed_on=today,
                change_type=MrrChange.TYPE_FREQUENCY,
                old_value=plan.frequency,
                new_value=det.frequency,
                monthly_delta=_q(det.monthly_value - old_monthly) if plan.manual_monthly_value is None else Decimal(0),
            )
        )
    if plan.detected_status != det.status:
        changes.append(
            MrrChange(
                plan=plan,
                changed_on=today,
                change_type=MrrChange.TYPE_STATUS,
                old_value=plan.detected_status,
                new_value=det.status,
                note="Detected from Jobber schedule",
            )
        )


def _history_value(det: Detection, month: date, today: date, carry):
    """
    MRR at the end of a month before tracking started, read the same way as today:
    from invoices, or from the visits scheduled after that month end (as today's value
    is read from the visits after today). A client whose schedule didn't change gets
    the same value in every month, so only real price/frequency changes show up.
    """
    hi = month_end(month)
    if det.pricing_source == PRICING_INVOICE:
        billed = invoiced_monthly(det.invoices, min(hi, today), det.start_date)
        if billed:
            _, latest, per_month, _ = billed
            return _q(latest * per_month), (latest, per_month)
        return (_q(carry[0] * carry[1]), carry) if carry else (det.monthly_value, (det.visit_value, Decimal(1)))
    price, freq = _schedule_terms([r for r in det.rows if r.day > hi], [r for r in det.rows if r.day <= hi])
    price = price or (carry[0] if carry else None) or det.visit_value
    per_month = freq[1] if freq else (carry[1] if carry else det.visits_per_month)
    return _q(Decimal(price) * per_month), (price, per_month)


def _in_month(det_start, det_end, month: date, today: date) -> bool:
    cutoff = min(month_end(month), today)
    if det_start is None or det_start > cutoff:
        return False
    return det_end is None or det_end > cutoff


def rebuild_mrr(integration, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    current = month_start(today)
    track_month = tracking_month()
    # Internal jobs (team leader tasks, inventory restock…) are not revenue.
    RecurringPlan.objects.filter(integration=integration, client__is_internal=True).delete()
    jobs = list(
        Job.objects.filter(integration=integration, is_recurring=True).exclude(client__is_internal=True).select_related("client")
    )
    visits = _visit_rows(integration, [job.id for job in jobs])
    invoices = _invoice_rows(integration, [job.id for job in jobs])
    existing = {plan.job_id: plan for plan in RecurringPlan.objects.filter(integration=integration)}
    now = timezone.now()

    changes, detections, plans = [], {}, []
    for job in jobs:
        det = detect(job, visits.get(job.id, []), today, invoices.get(job.id, []))
        plan = existing.get(job.id) or RecurringPlan(integration=integration, job=job)
        is_new = plan.pk is None
        _log_changes(plan, det, today, changes)
        plan.client = job.client
        plan.name = (job.client.name if job.client else "") or job.title or f"Job #{job.job_number}"
        plan.division = job.division or (job.client.division if job.client else "")
        plan.service = (job.title or "")[:255]
        plan.frequency = det.frequency
        plan.visits_per_month = det.visits_per_month
        plan.visit_value = det.visit_value
        plan.monthly_value = det.monthly_value
        plan.pricing_source = det.pricing_source
        plan.start_date = det.start_date
        if plan.status_override == STATUS_CANCELLED:
            plan.end_date = plan.end_date or det.end_date or today
        else:
            plan.end_date = det.end_date
        plan.detected_status = det.status
        plan.last_detected_at = now
        plan.save()
        if is_new and det.start_date and det.start_date >= track_month:
            changes.append(
                MrrChange(plan=plan, changed_on=det.start_date, change_type=MrrChange.TYPE_NEW, new_value=str(det.monthly_value), monthly_delta=det.monthly_value)
            )
        detections[plan.id] = det
        plans.append(plan)

    with transaction.atomic():
        MrrChange.objects.bulk_create(changes)
        snapshots = _build_snapshots(plans, detections, today, current, track_month)
        MrrSnapshot.objects.filter(plan__integration=integration, source=MrrSnapshot.SOURCE_HISTORY).delete()
        MrrSnapshot.objects.filter(plan__integration=integration, month=current).delete()
        MrrSnapshot.objects.bulk_create(snapshots, batch_size=2000)

    return {"plans": len(plans), "changes": len(changes), "snapshots": len(snapshots)}


def _build_snapshots(plans, detections, today, current, track_month):
    tracked = defaultdict(dict)
    for snap in MrrSnapshot.objects.filter(plan__in=plans, source=MrrSnapshot.SOURCE_TRACKED).exclude(month=current):
        tracked[snap.plan_id][snap.month] = snap

    out = []
    for plan in plans:
        det = detections[plan.id]
        start = det.start_date
        if start is None:
            continue
        carry = None
        for month in months_between(start, today):
            if month == current:
                status = plan.status
                # Not cancelled = detected active (no end date) or kept alive by a manual status.
                if status == STATUS_CANCELLED or plan.start_date is None or plan.start_date > today:
                    continue
                out.append(
                    MrrSnapshot(
                        plan=plan,
                        month=month,
                        monthly_value=_q(plan.effective_monthly_value),
                        status=status,
                        source=MrrSnapshot.SOURCE_TRACKED,
                    )
                )
                continue
            if not _in_month(det.start_date, det.end_date, month, today):
                continue
            if month >= track_month:
                if month in tracked[plan.id]:
                    continue  # keep what was recorded at the time
                value, carry = _history_value(det, month, today, carry)
                out.append(MrrSnapshot(plan=plan, month=month, monthly_value=value, status=STATUS_ACTIVE, source=MrrSnapshot.SOURCE_TRACKED))
                continue
            value, carry = _history_value(det, month, today, carry)
            out.append(MrrSnapshot(plan=plan, month=month, monthly_value=value, status=STATUS_ACTIVE, source=MrrSnapshot.SOURCE_HISTORY))
    return out


def refresh_current_snapshot(plan: RecurringPlan, today: date | None = None):
    """After a manual edit, rewrite this plan's current-month snapshot immediately."""
    today = today or timezone.localdate()
    current = month_start(today)
    MrrSnapshot.objects.filter(plan=plan, month=current).delete()
    if plan.status == STATUS_CANCELLED or not plan.start_date or plan.start_date > today:
        return
    MrrSnapshot.objects.create(
        plan=plan, month=current, monthly_value=_q(plan.effective_monthly_value), status=plan.status, source=MrrSnapshot.SOURCE_TRACKED
    )


# ——— Period analysis ———

MOVEMENT_TYPES = ("new", "expansion", "reactivated", "resumed", "contraction", "churned", "paused", "untracked")


@dataclass
class Movements:
    available: bool
    starting: Decimal = Decimal(0)
    ending: Decimal = Decimal(0)
    paused_ending: Decimal = Decimal(0)
    totals: dict = field(default_factory=lambda: {key: Decimal(0) for key in MOVEMENT_TYPES})
    rows: list = field(default_factory=list)
    recurring_start: set = field(default_factory=set)
    recurring_end: set = field(default_factory=set)
    paused_end: set = field(default_factory=set)
    expansion_tracked_from: date | None = None

    @property
    def net_new(self) -> Decimal:
        return self.ending - self.starting


def _client_key(plan_id, client_id):
    return f"c{client_id}" if client_id else f"p{plan_id}"


def movements(integration, start: date, end: date, division: str = "") -> Movements:
    months = months_between(start, end)
    before = add_months(months[0], -1)
    snaps = MrrSnapshot.objects.filter(plan__integration=integration, month__gte=before, month__lte=months[-1])
    if division:
        snaps = snaps.filter(plan__division=division)
    first_month = MrrSnapshot.objects.filter(plan__integration=integration).aggregate(m=Min("month"))["m"]
    if first_month is None or months[-1] < first_month:
        return Movements(available=False)

    active = defaultdict(lambda: defaultdict(Decimal))  # month -> client -> value
    paused = defaultdict(lambda: defaultdict(Decimal))
    names = {}
    for snap in snaps.select_related("plan").iterator(chunk_size=2000):
        key = _client_key(snap.plan_id, snap.plan.client_id)
        names.setdefault(key, {"client_id": snap.plan.client_id, "name": snap.plan.name, "division": snap.plan.division, "service": snap.plan.frequency})
        bucket = paused if snap.status in PAUSED_STATUSES else active
        bucket[snap.month][key] += snap.monthly_value

    # Clients with any MRR before a month → a return is a reactivation, not a new client.
    first_seen = {}
    history = MrrSnapshot.objects.filter(plan__integration=integration, month__lt=months[-1])
    if division:
        history = history.filter(plan__division=division)
    for plan_id, client_id, month in history.values_list("plan_id", "plan__client_id", "month").order_by().distinct():
        key = _client_key(plan_id, client_id)
        if key not in first_seen or month < first_seen[key]:
            first_seen[key] = month

    track_month = tracking_month()
    result = Movements(available=True, expansion_tracked_from=add_months(track_month, 1))
    result.starting = sum(active[before].values(), Decimal(0))
    result.ending = sum(active[months[-1]].values(), Decimal(0))
    result.paused_ending = sum(paused[months[-1]].values(), Decimal(0))
    result.recurring_start = set(active[before]) | set(paused[before])
    result.recurring_end = set(active[months[-1]]) | set(paused[months[-1]])
    result.paused_end = set(paused[months[-1]]) - set(active[months[-1]])

    prev = before
    for month in months:
        keys = set(active[prev]) | set(active[month]) | set(paused[prev]) | set(paused[month])
        tracked = prev >= track_month
        for key in keys:
            a_prev, a_cur = active[prev].get(key, Decimal(0)), active[month].get(key, Decimal(0))
            p_prev, p_cur = paused[prev].get(key, Decimal(0)), paused[month].get(key, Decimal(0))
            if a_prev == a_cur:
                continue
            if a_prev == 0:
                kind = "resumed" if p_prev > 0 else ("reactivated" if first_seen.get(key, month) < prev else "new")
            elif a_cur == 0:
                kind = "paused" if p_cur > 0 else "churned"
            elif not tracked:
                kind = "untracked"
            else:
                kind = "expansion" if a_cur > a_prev else "contraction"
            delta = a_cur - a_prev
            result.totals[kind] += delta
            info = names.get(key, {})
            result.rows.append(
                {
                    "type": kind,
                    "month": month.isoformat(),
                    "client_id": info.get("client_id"),
                    "client": info.get("name") or "—",
                    "division": info.get("division") or "",
                    "service": info.get("service") or "",
                    "before": _q(a_prev),
                    "after": _q(a_cur),
                    "change": _q(delta),
                }
            )
        prev = month
    return result
