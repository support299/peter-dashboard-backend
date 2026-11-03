"""Expense totals for any period.

Amounts are before tax: GST and QST paid on expenses are claimed back, so they
are not a cost. Rules:
- Expenses created by a recurring expense count once they're verified.
- A monthly total for a category replaces that category's individual expenses
  for the month (no double counting). When a period covers only part of a
  month, the monthly total is counted pro rata by days.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Min, Sum
from django.db.models.functions import TruncMonth

from financials.models import (
    DIVISION_SHARED,
    GROUP_COGS,
    GROUP_OPEX,
    GROUP_OTHER,
    GROUP_VEHICLES,
    Category,
    Expense,
    MonthlyTotal,
    MonthReview,
    PayRun,
)

ZERO = Decimal("0")
PARTS = ("cogs", "opex", "vehicles", "financial", "excluded", "payroll", "labour")


def _month(value: date) -> date:
    return value.replace(day=1)


def _month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def _overlap_share(month: date, start: date, end: date) -> Decimal:
    lo, hi = max(month, start), min(_month_end(month), end)
    if hi < lo:
        return ZERO
    days = calendar.monthrange(month.year, month.month)[1]
    return Decimal((hi - lo).days + 1) / Decimal(days)


def categories() -> dict:
    return {c.id: c for c in Category.objects.all()}


def rows(start: date, end: date, division: str | None = None) -> list[dict]:
    """[{month, category_id, subcategory_id, division, amount, from_total}] for [start, end]."""
    totals = list(
        MonthlyTotal.objects.filter(month__gte=_month(start), month__lte=end).values("month", "category_id", "division", "amount")
    )
    replaced = {(t["month"], t["category_id"]) for t in totals}

    individual = (
        Expense.objects.filter(date__gte=start, date__lte=end, pending_verification=False)
        .annotate(m=TruncMonth("date"))
        .values("m", "category_id", "subcategory_id", "division")
        .annotate(s=Sum("amount"))
        .order_by()
    )
    out = []
    for row in individual:
        month = row["m"] if isinstance(row["m"], date) else row["m"].date()
        if (month, row["category_id"]) in replaced:
            continue
        out.append(
            {
                "month": month,
                "category_id": row["category_id"],
                "subcategory_id": row["subcategory_id"],
                "division": row["division"],
                "amount": row["s"] or ZERO,
                "from_total": False,
            }
        )
    for t in totals:
        share = _overlap_share(t["month"], start, end)
        if share:
            out.append(
                {
                    "month": t["month"],
                    "category_id": t["category_id"],
                    "subcategory_id": None,
                    "division": t["division"],
                    "amount": (t["amount"] * share).quantize(Decimal("0.01")),
                    "from_total": True,
                }
            )
    if division:
        out = [r for r in out if r["division"] == division]
    return out


def part_of(category: Category) -> str:
    if category.group == GROUP_COGS:
        return "cogs"
    if category.group == GROUP_OPEX:
        return "opex"
    if category.group == GROUP_VEHICLES:
        return "vehicles"
    return "financial" if category.in_profit_and_loss else "excluded"


def summarize(items: list[dict], cats: dict | None = None) -> dict:
    cats = cats or categories()
    parts = {key: ZERO for key in PARTS}
    for row in items:
        cat = cats[row["category_id"]]
        parts[part_of(cat)] += row["amount"]
        if cat.counts_as_payroll:
            parts["payroll"] += row["amount"]
        if cat.counts_as_labour:
            parts["labour"] += row["amount"]
    return parts


def summary(start: date, end: date, division: str | None = None) -> dict:
    return summarize(rows(start, end, division))


def monthly_parts(first: date, last: date) -> dict:
    """{month: parts} from the month of `first` to the end of the month of `last`."""
    cats = categories()
    by_month = defaultdict(list)
    for row in rows(_month(first), _month_end(last)):
        by_month[row["month"]].append(row)
    return {month: summarize(items, cats) for month, items in by_month.items()}


def total_expenses(parts: dict) -> Decimal:
    """Everything in profit & loss (loan principal, transfers, equipment purchases… are left out)."""
    return parts["cogs"] + parts["opex"] + parts["vehicles"] + parts["financial"]


def _pct(part, whole):
    if part is None or not whole:
        return None
    return (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1"))


def kpi(key: str, parts: dict, revenue) -> Decimal | None:
    """One financial KPI from expense parts and revenue (revenue None = unknown)."""
    direct = {
        "total_expenses": total_expenses(parts),
        "cogs": parts["cogs"],
        "operating_expenses": parts["opex"],
        "vehicle_expenses": parts["vehicles"],
        "financial_costs": parts["financial"],
        "payroll": parts["payroll"],
        "labour_cost": parts["labour"],
    }
    if key in direct:
        return direct[key]
    if revenue is None:
        return None
    revenue = Decimal(revenue)
    gross = revenue - parts["cogs"]
    operating = gross - parts["opex"] - parts["vehicles"]
    net = operating - parts["financial"]
    return {
        "gross_profit": gross,
        "gross_margin": _pct(gross, revenue),
        "operating_profit": operating,
        "operating_margin": _pct(operating, revenue),
        "net_profit": net,
        "net_margin": _pct(net, revenue),
        "cogs_pct": _pct(parts["cogs"], revenue),
        "opex_pct": _pct(parts["opex"], revenue),
        "payroll_pct": _pct(parts["payroll"], revenue),
        "labour_cost_pct": _pct(parts["labour"], revenue),
        "expenses_pct": _pct(total_expenses(parts), revenue),
    }.get(key)


FINANCE_KPIS = (
    "total_expenses",
    "cogs",
    "operating_expenses",
    "vehicle_expenses",
    "financial_costs",
    "payroll",
    "labour_cost",
    "gross_profit",
    "gross_margin",
    "operating_profit",
    "operating_margin",
    "net_profit",
    "net_margin",
    "cogs_pct",
    "opex_pct",
    "payroll_pct",
    "labour_cost_pct",
    "expenses_pct",
)
PROFIT_KPIS = {"gross_profit", "gross_margin", "operating_profit", "operating_margin", "net_profit", "net_margin"}
# Need revenue and costs for the same dates: not shown for a period that starts before expenses were entered,
# or months of revenue would be set against no costs (a fake profit).
REVENUE_KPIS = PROFIT_KPIS | {"cogs_pct", "opex_pct", "payroll_pct", "labour_cost_pct", "expenses_pct"}


def covers(start: date, end: date, first: date | None, key: str) -> bool:
    """Whether expense data covers [start, end] enough to show this KPI."""
    if not first:
        return False
    if key in REVENUE_KPIS:
        return start >= _month(first)
    return end >= first


def data_start() -> date | None:
    """First day with any expense information."""
    candidates = [
        Expense.objects.filter(pending_verification=False).aggregate(m=Min("date"))["m"],
        MonthlyTotal.objects.aggregate(m=Min("month"))["m"],
        PayRun.objects.aggregate(m=Min("pay_date"))["m"],
    ]
    candidates = [c for c in candidates if c]
    return min(candidates) if candidates else None


def unreviewed_months(start: date, end: date) -> list[date]:
    months, cursor = [], _month(start)
    while cursor <= end:
        months.append(cursor)
        cursor = _month_end(cursor) + timedelta(days=1)
    reviewed = set(MonthReview.objects.filter(month__in=months, status=MonthReview.STATUS_REVIEWED).values_list("month", flat=True))
    return [m for m in months if m not in reviewed]


def is_locked(day: date | None) -> bool:
    """Reviewed months are closed: reopen them in Month review to change anything."""
    if not day:
        return False
    return MonthReview.objects.filter(month=_month(day), status=MonthReview.STATUS_REVIEWED).exists()


def by_category(start: date, end: date, division: str | None = None) -> dict:
    """{category_id: amount} (subcategories rolled into their category)."""
    out = defaultdict(lambda: ZERO)
    for row in rows(start, end, division):
        out[row["category_id"]] += row["amount"]
    return dict(out)


def by_division(start: date, end: date) -> dict:
    """{division: parts}; 'shared' holds costs not tied to one division."""
    cats = categories()
    grouped = defaultdict(list)
    for row in rows(start, end):
        grouped[row["division"] or DIVISION_SHARED].append(row)
    return {division: summarize(items, cats) for division, items in grouped.items()}


GROUP_PART = {GROUP_COGS: "cogs", GROUP_OPEX: "opex", GROUP_VEHICLES: "vehicles", GROUP_OTHER: "financial"}
