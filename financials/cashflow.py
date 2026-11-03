"""Money out for the cash flow estimate (Financials access only).

Paid expenses with tax, by expense date, including money that isn't a cost
(loan principal, equipment, tax remittances). Left out: credit card payments and
bank transfers, which only move money between our own accounts (the card
purchases themselves are already counted). Where a monthly total replaces a
category's expenses, the total is used (it has no tax, so it's as entered).
"""

from __future__ import annotations

from decimal import Decimal

from financials import ledger
from financials.models import STATUS_PAID, Category, Expense, MonthlyTotal

INTERNAL_MOVES = {"credit_card_payments", "bank_transfers"}


def money_out(start, end) -> Decimal:
    skip = set(Category.objects.filter(system_key__in=INTERNAL_MOVES).values_list("id", flat=True))
    totals = MonthlyTotal.objects.filter(month__gte=start.replace(day=1), month__lte=end).exclude(category_id__in=skip)
    replaced = {(t.month, t.category_id) for t in totals}
    out = Decimal(0)
    for day, category_id, total in Expense.objects.filter(
        date__gte=start, date__lte=end, pending_verification=False, status=STATUS_PAID
    ).exclude(category_id__in=skip).values_list("date", "category_id", "total"):
        if (day.replace(day=1), category_id) not in replaced:
            out += total
    for t in totals:
        out += (t.amount * ledger._overlap_share(t.month, start, end)).quantize(Decimal("0.01"))
    return out
