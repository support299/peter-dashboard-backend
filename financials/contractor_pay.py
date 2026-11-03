"""Contractor pay from the Hub, added to expenses automatically.

The Hub works out each contractor's pay per visit (Peter, Nov 2026). From the start
date in CONTRACTOR_PAY_START, every day's pay, tips and late adjustments become one
expense per division under Subcontractors (counted as labour, not PayEvo payroll),
and Peter stops entering contractor pay by hand. Supplies and restock deductions are
money back for equipment: they stay out of expenses and only show on the contractor
pay page.

A day in a month that was already reviewed is never changed: if its pay still moves
(a week that spans the month end is paid later), the difference is booked today.

The expenses are always "paid": the cost belongs to the visit date, and a week that's
still open would otherwise stay "unpaid" forever once its month is reviewed (and drop
out of cash flow). Whether a week is paid yet shows on the contractor pay page.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from analytics.hub_models import HubContractorPay
from analytics.parsing import parse_d
from financials import ledger
from financials.models import DIVISION_SHARED, EXPENSE_DIVISION_CHOICES, SOURCE_HUB, STATUS_PAID, Category, Expense, MonthlyTotal
from operations.models import Visit

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
CATEGORY_KEY = "subcontractors"
VENDOR = "Contractors (Hub)"
PREFIX = "contractor_pay"
DIVISIONS = {key for key, _ in EXPENSE_DIVISION_CHOICES}


def start_date() -> date | None:
    """The first day contractor pay is added automatically (empty = not switched on)."""
    return parse_d(getattr(settings, "CONTRACTOR_PAY_START", ""))


def category() -> Category | None:
    return Category.objects.filter(system_key=CATEGORY_KEY).first()


def _key(day: date, division: str) -> str:
    return f"{PREFIX}:{day.isoformat()}:{division}"


def _late_key(day: date, division: str, booked_on: date) -> str:
    return f"{_key(day, division)}:late:{booked_on.isoformat()}"


def _origin(external_id: str) -> tuple[date, str] | None:
    parts = external_id.split(":")
    if len(parts) < 3 or parts[0] != PREFIX:
        return None
    day = parse_d(parts[1])
    return (day, parts[2]) if day else None


def daily_totals(start: date) -> dict:
    """{(day, division): {"amount", "lines"}} for pay, tips and adjustments from `start`."""
    lines = list(HubContractorPay.objects.filter(line_type__in=HubContractorPay.EXPENSE_TYPES, date__gte=start))
    visit_ids = {line.jobber_visit_id for line in lines if line.jobber_visit_id}
    division_of = dict(Visit.objects.filter(external_id__in=visit_ids).values_list("external_id", "division")) if visit_ids else {}
    totals = defaultdict(lambda: {"amount": ZERO, "lines": 0})
    for line in lines:
        division = division_of.get(line.jobber_visit_id) or DIVISION_SHARED
        bucket = totals[(line.date, division if division in DIVISIONS else DIVISION_SHARED)]
        bucket["amount"] += Decimal(line.amount)
        if line.status != "void":
            bucket["lines"] += 1
    return totals


def _write(expense: Expense, day: date, amount: Decimal, notes: str = "") -> None:
    expense.date = day
    expense.vendor = VENDOR
    expense.description = "Contractor pay · late change" if notes else "Contractor pay"
    expense.amount, expense.gst, expense.qst = amount, ZERO, ZERO
    expense.status = STATUS_PAID
    expense.notes = notes or "Added automatically from contractor pay in the Hub. Change it in the Hub."
    expense.save()


@transaction.atomic
def book_expenses(today: date | None = None) -> dict | None:
    """Bring the automatic contractor pay expenses in line with the Hub. Safe to run any number of times."""
    start = start_date()
    cat = category()
    if not start or not cat:
        if start:
            logger.warning("Contractor pay: no Subcontractors category (system key %r)", CATEGORY_KEY)
        return None
    today = today or timezone.localdate()
    totals = daily_totals(start)

    existing = {e.external_id: e for e in Expense.objects.filter(source=SOURCE_HUB, external_id__startswith=f"{PREFIX}:")}
    late = defaultdict(list)
    for external_id, expense in existing.items():
        if ":late:" in external_id:
            late[_origin(external_id)].append(expense)
    keys = set(totals) | {_origin(eid) for eid in existing if ":late:" not in eid and _origin(eid)} | set(late)
    stats = {"days": 0, "created": 0, "updated": 0, "deleted": 0, "late": 0, "skipped_locked": 0}

    for day, division in sorted(keys):
        info = totals.get((day, division), {"amount": ZERO, "lines": 0})
        target = info["amount"].quantize(Decimal("0.01"))
        original = existing.get(_key(day, division))
        corrections = late.get((day, division), [])
        stats["days"] += 1

        if not ledger.is_locked(day):
            # Open month: one expense holds the day. Late changes booked in months still open fold back in.
            for expense in [c for c in corrections if not ledger.is_locked(c.date)]:
                expense.delete()
                corrections.remove(expense)
                stats["deleted"] += 1
            amount = target - sum((c.amount for c in corrections), ZERO)
            if not amount:  # nothing to pay yet (e.g. no % in the Hub): no $0 expense
                if original:
                    original.delete()
                    stats["deleted"] += 1
                continue
            if original and original.amount == amount:
                continue
            stats["updated" if original else "created"] += 1
            _write(original or Expense(source=SOURCE_HUB, external_id=_key(day, division), category=cat, division=division), day, amount)
            continue

        # Reviewed month: leave it as it is and book any difference today.
        booked = (original.amount if original else ZERO) + sum((c.amount for c in corrections), ZERO)
        difference = target - booked
        if not difference:
            continue
        if ledger.is_locked(today):
            stats["skipped_locked"] += 1
            logger.warning("Contractor pay for %s changed by %s, but this month is reviewed too", day, difference)
            continue
        key = _late_key(day, division, today)
        expense = existing.get(key) or Expense(source=SOURCE_HUB, external_id=key, category=cat, division=division, amount=ZERO)
        amount = expense.amount + difference
        if not amount:
            if expense.pk:
                expense.delete()
            continue
        stats["late"] += 1
        note = f"Late change to contractor pay for {day:%b} {day.day}, {day.year}, which is in a reviewed month."
        _write(expense, today, amount, notes=note)
    return stats


# ——— duplicates: from the start date, contractor pay is never entered by hand ———
def duplicate_warning(day: date | None, category_id: int | None, source: str = "") -> str:
    start = start_date()
    if not (start and day and category_id) or source == SOURCE_HUB or day < start:
        return ""
    cat = category()
    if not cat or category_id != cat.id:
        return ""
    since = f"{start:%b} {start.day}, {start.year}"
    return (
        f"Contractor pay for work from {since} is added automatically from the Hub. "
        f"If this is contractor pay for work on or after {since}, it's counted twice. Pay for earlier work is fine."
    )


def expense_warning(expense: Expense) -> str:
    return duplicate_warning(expense.date, expense.category_id, expense.source)


def monthly_total_warning(total: MonthlyTotal) -> str:
    start = start_date()
    if not start or total.month < start.replace(day=1):
        return ""
    return duplicate_warning(max(total.month, start), total.category_id)


# ——— contractor pay page ———
def summary(start: date, end: date) -> dict:
    """Per contractor: visits, pay, tips, adjustments, deductions and what's still missing."""
    lines = HubContractorPay.objects.filter(date__gte=start, date__lte=end).order_by("date", "contractor_name")
    people = {}
    totals = defaultdict(lambda: ZERO)
    missing, visits = 0, set()
    known = {*HubContractorPay.EXPENSE_TYPES, *HubContractorPay.DEDUCTION_TYPES}
    for line in lines:
        if line.line_type not in known:
            continue  # a type this version doesn't know: kept out of the page, as it is out of expenses
        person = people.setdefault(
            line.contractor_external_id or line.contractor_name,
            {
                "key": line.contractor_external_id or line.contractor_name,
                "contractor": line.contractor_name or "Unknown contractor",
                "visits": set(),
                "pay": ZERO,
                "tips": ZERO,
                "adjustments": ZERO,
                "deductions": ZERO,
                "unpaid": ZERO,
                "missing_percent": 0,
            },
        )
        amount = Decimal(line.amount)
        if line.line_type in HubContractorPay.DEDUCTION_TYPES:
            person["deductions"] += amount
            totals["deductions"] += amount
            continue
        field = {"pay": "pay", "tip": "tips"}.get(line.line_type, "adjustments")
        person[field] += amount
        totals[field] += amount
        if line.line_type == "pay" and line.status != "void" and line.jobber_visit_id:
            person["visits"].add(line.jobber_visit_id)
            visits.add(line.jobber_visit_id)
        if not line.week_paid:
            person["unpaid"] += amount
            totals["unpaid"] += amount
        if line.status != "void" and "missing_percent" in line.flags:
            person["missing_percent"] += 1
            missing += 1

    rows = []
    for person in people.values():
        person["visits"] = len(person["visits"])
        person["expense"] = person["pay"] + person["tips"] + person["adjustments"]
        person["net"] = person["expense"] + person["deductions"]
        rows.append(person)
    rows.sort(key=lambda r: (-r["expense"], r["contractor"]))
    expense = totals["pay"] + totals["tips"] + totals["adjustments"]
    return {
        "rows": rows,
        "totals": {
            "visits": len(visits),  # a visit two contractors share counts once
            "pay": totals["pay"],
            "tips": totals["tips"],
            "adjustments": totals["adjustments"],
            "deductions": totals["deductions"],
            "expense": expense,
            "net": expense + totals["deductions"],
            "unpaid": totals["unpaid"],
            "missing_percent": missing,
        },
    }


def lines(start: date, end: date, contractor: str = "") -> list[dict]:
    known = [*HubContractorPay.EXPENSE_TYPES, *HubContractorPay.DEDUCTION_TYPES]
    qs = HubContractorPay.objects.filter(date__gte=start, date__lte=end, line_type__in=known).order_by("date", "contractor_name", "line_type")
    if contractor:
        qs = qs.filter(Q(contractor_external_id=contractor) | Q(contractor_external_id="", contractor_name=contractor))
    return [
        {
            "id": line.external_id,
            "date": line.date,
            "type": line.line_type,
            "contractor": line.contractor_name,
            "client": line.client_name,
            "job": line.job_title,
            "amount_paid": line.amount,
            "calculated_amount": line.calculated_amount,
            "status": line.status,
            "week_start": line.week_start,
            "week_paid": line.week_paid,
            "pay_rule": line.pay_rule,
            "changed_by": line.changed_by,
            "reason": line.reason,
            "flags": line.flags,
        }
        for line in qs[:2000]
    ]
