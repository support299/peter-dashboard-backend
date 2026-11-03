"""Recurring expenses: create one expense per due date, waiting to be verified.

A created expense doesn't count in totals until someone checks it against the
real charge (and corrects the amount if needed). One expense per due date is
enforced by a unique constraint, so running this twice never duplicates.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from financials.models import SOURCE_RECURRING, STATUS_UNPAID, Expense, RecurringExpense
from insights.periods import add_months

LOOKAHEAD_DAYS = 7  # show charges coming up this week, so they can be checked when they land
# A start date years ago must not create dozens of charges to verify: older ones are entered as expenses or imported.
LOOKBACK_DAYS = 62
MAX_PER_RUN = 400

STEP_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}
STEP_DAYS = {"weekly": 7, "biweekly": 14}


def due_dates(rec: RecurringExpense, until: date) -> list[date]:
    last = min(until, rec.end_date) if rec.end_date else until
    out, n = [], 0
    while len(out) < MAX_PER_RUN:
        if rec.frequency in STEP_DAYS:
            day = rec.start_date + timedelta(days=STEP_DAYS[rec.frequency] * n)
        else:
            # Always step from the start date so the 31st doesn't drift to the 28th.
            day = add_months(rec.start_date, STEP_MONTHS.get(rec.frequency, 1) * n)
        if day > last:
            break
        out.append(day)
        n += 1
    return out


def expense_fields(rec: RecurringExpense) -> dict:
    return {
        "vendor": rec.vendor,
        "description": rec.description or rec.name,
        "amount": rec.amount,
        "gst": rec.gst,
        "qst": rec.qst,
        "category_id": rec.category_id,
        "subcategory_id": rec.subcategory_id,
        "division": rec.division,
        "payment_method": rec.payment_method,
    }


def generate(rec: RecurringExpense, today: date | None = None) -> int:
    if not rec.is_active:
        return 0
    today = today or timezone.localdate()
    skipped = set(rec.skipped_dates or [])
    existing = set(rec.expenses.values_list("due_date", flat=True))
    created = 0
    for day in due_dates(rec, today + timedelta(days=LOOKAHEAD_DAYS)):
        if day < today - timedelta(days=LOOKBACK_DAYS) or day in existing or day.isoformat() in skipped:
            continue
        try:
            with transaction.atomic():
                Expense.objects.create(
                    **expense_fields(rec),
                    date=day,
                    due_date=day,
                    recurring=rec,
                    source=SOURCE_RECURRING,
                    status=STATUS_UNPAID,
                    pending_verification=True,
                    created_by=rec.created_by,
                )
            created += 1
        except IntegrityError:
            continue
    return created


def generate_all(today: date | None = None) -> int:
    return sum(generate(rec, today) for rec in RecurringExpense.objects.filter(is_active=True))


def sync_pending(rec: RecurringExpense) -> None:
    """After the recurring expense is edited: update unverified entries, drop ones it no longer covers."""
    pending = rec.expenses.filter(pending_verification=True)
    pending.update(**expense_fields(rec))
    for expense in pending:
        expense.save(update_fields=["total"])  # recompute total
    if not rec.is_active:
        pending.filter(date__gt=timezone.localdate()).delete()
    else:
        valid = set(due_dates(rec, timezone.localdate() + timedelta(days=LOOKAHEAD_DAYS)))
        pending.exclude(due_date__in=valid).delete()
