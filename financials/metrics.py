"""Financial KPIs for the Master Dashboard (insights.metrics.Metrics mixes this in).

Visible only to people with Financials access (full or admin), and only for all
divisions: shared costs (rent, office payroll…) aren't split across divisions.
"""

from __future__ import annotations

from decimal import Decimal

from financials import ledger
from financials.models import GROUP_CHOICES, Expense, MonthlyTotal

NO_ACCESS_NOTE = "Only people with Financials access can see this."
DIVISION_NOTE = "Shown for all divisions only: shared costs aren't split by division. Financials → Profit & Loss shows each division's direct costs."


class FinanceMetrics:
    finance_visible = False

    def finance_parts(self, period) -> dict:
        key = ("finance_parts", period.start, period.end)
        if key not in self._cache:
            self._cache[key] = ledger.summary(period.start, period.end)
        return self._cache[key]

    def _finance(self, key, period):
        if not self.finance_visible or self.division:
            return None
        if not ledger.covers(period.start, period.end, self.data_starts().get("expenses"), key):
            return None
        parts = self.finance_parts(period)
        # A month with revenue and no expenses entered would show everything as profit.
        if key in ledger.REVENUE_KPIS and not any(parts.values()):
            return None
        return ledger.kpi(key, parts, self.value("revenue", period))

    def finance_note(self, key, period):
        if not self.finance_visible:
            return NO_ACCESS_NOTE
        if self.division:
            return DIVISION_NOTE
        if key in ("cash_in", "cash_out", "net_cash_flow") and not self.data_starts().get("payments"):
            from insights.metrics import jobber_sync_note

            return jobber_sync_note("payments")
        if key in ("cost_per_lead", "cost_per_new_client") and self.value("leads" if key == "cost_per_lead" else "new_clients", period) == 0:
            return "No leads in this period." if key == "cost_per_lead" else "No new clients in this period."
        start = self.data_starts().get("expenses")
        if not start:
            return "No expenses entered yet. Add them in Financials."
        if key in ledger.REVENUE_KPIS and period.start >= start.replace(day=1) and not any(self.finance_parts(period).values()):
            return "No expenses entered for this period, so this can't be calculated."
        if key in ledger.REVENUE_KPIS and period.end >= start and period.start < start.replace(day=1):
            return (
                f"Expenses start on {start:%b} {start.day}, {start.year}, so this can't be calculated for a period that begins earlier "
                f"(it would set earlier revenue against no costs). Pick a period from {start:%B %Y} on."
            )
        if key in ledger.REVENUE_KPIS and self.value("revenue", period) is None:
            return "No revenue data for this period."
        return f"Expenses start on {start:%b} {start.day}, {start.year}."

    def unreviewed_note(self, period):
        months = ledger.unreviewed_months(period.start, period.end)
        if not months:
            return None
        names = ", ".join(f"{m:%b %Y}" for m in months[:3]) + ("…" if len(months) > 3 else "")
        return f"Not reviewed yet: {names}."


for _key in ledger.FINANCE_KPIS:
    setattr(FinanceMetrics, f"_{_key}", (lambda k: lambda self, period: self._finance(k, period))(_key))


# ——— drill-down: the expenses behind a number ———
GROUP_LABELS = dict(GROUP_CHOICES)
DRILL_PARTS = {
    "expenses": ("Expenses", None),
    "expenses_cogs": ("Cost of sales (COGS)", "cogs"),
    "expenses_opex": ("Operating expenses", "opex"),
    "expenses_vehicles": ("Vehicle expenses", "vehicles"),
    "expenses_financial": ("Interest & financing costs", "financial"),
    "expenses_payroll": ("Payroll", "payroll"),
    "expenses_labour": ("Labour cost", "labour"),
    "expenses_marketing": ("Marketing spend", "marketing"),
}


def _matches(category, part) -> bool:
    if part is None:
        return ledger.part_of(category) != "excluded"
    if part == "payroll":
        return category.counts_as_payroll
    if part == "labour":
        return category.counts_as_labour
    if part == "marketing":
        return category.system_key == "marketing"
    return ledger.part_of(category) == part


def expense_drill(metrics, period, key) -> dict:
    title, part = DRILL_PARTS[key]
    columns = [
        {"key": "date", "label": "Date", "kind": "date"},
        {"key": "vendor", "label": "Vendor"},
        {"key": "description", "label": "Description"},
        {"key": "category", "label": "Category"},
        {"key": "division", "label": "Division"},
        {"key": "amount", "label": "Before tax", "kind": "currency"},
    ]
    if not metrics.finance_visible:
        return {"title": title, "columns": columns, "rows": [], "total_rows": 0, "note": NO_ACCESS_NOTE}
    cats = ledger.categories()
    totals = MonthlyTotal.objects.filter(month__gte=period.start.replace(day=1), month__lte=period.end)
    replaced = {(t.month, t.category_id) for t in totals}
    rows = []
    qs = Expense.objects.filter(date__gte=period.start, date__lte=period.end, pending_verification=False).select_related("category", "subcategory")
    for e in qs.order_by("-date", "-id"):
        if (e.date.replace(day=1), e.category_id) in replaced or not _matches(cats[e.category_id], part):
            continue
        rows.append(
            {
                "date": e.date,
                "vendor": e.vendor or "—",
                "description": e.description,
                "category": str(e.subcategory or e.category),
                "division": e.get_division_display(),
                "amount": e.amount,
            }
        )
    for t in totals.select_related("category"):
        share = ledger._overlap_share(t.month, period.start, period.end)
        if share and _matches(cats[t.category_id], part):
            rows.append(
                {
                    "date": t.month,
                    "vendor": "Monthly total",
                    "description": t.notes or f"{t.month:%B %Y}" + (" (part of month)" if share < 1 else ""),
                    "category": t.category.name,
                    "division": t.get_division_display(),
                    "amount": (t.amount * share).quantize(Decimal("0.01")),
                }
            )
    total = sum((r["amount"] for r in rows), Decimal(0))
    return {"title": title, "columns": columns, "rows": rows[:1000], "total_rows": len(rows), "total": total}
