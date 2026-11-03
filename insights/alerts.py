"""Alert limits on Master Dashboard numbers.

"Goes above / below" compares the number with the limit. Limits on totals (revenue,
counts, hours…) are monthly: they're scaled to the period's length (× 3 for a quarter,
× 12 for a year) and to the days so far while it's still running, so "revenue below
$60,000 a month" doesn't fire on the 3rd. Averages, rates, percentages and balances
are compared as they are. "Rises / falls by more than X%"
compares with the previous period or the same period last year. "Is higher / lower
than another number" compares two numbers of the same period, so no scaling applies.
"""

from __future__ import annotations

from decimal import Decimal

from insights.metrics import KPIS
from insights import periods
from insights.models import AlertRule

# Totals that grow through the period (scaled for a period in progress).
NOT_ADDITIVE = {"avg_revenue_per_visit", "avg_new_client_value", "avg_one_off_price", "revenue_per_labour_hour", "cost_per_lead", "cost_per_new_client"}
ADDITIVE_DECIMAL = {"labour_hours", "overtime_hours", "drive_time_hours", "touch_up_hours"}
DAYS_PER_MONTH = Decimal("365.25") / 12


def additive(key: str) -> bool:
    meta = KPIS[key]
    if meta["point_in_time"] or key in NOT_ADDITIVE:
        return False
    return meta["kind"] in ("currency", "count") or key in ADDITIVE_DECIMAL


def monthly_limit(limit: Decimal, period) -> Decimal:
    """A monthly limit for this period: its months, then the days with data so far."""
    full_days = (period.full_end - period.start).days + 1
    whole_months = period.start.day == 1 and period.full_end == periods.month_end(period.full_end)
    months = Decimal(len(periods.months_between(period.start, period.full_end))) if whole_months else full_days / DAYS_PER_MONTH
    # Days with data so far: a month in progress, or a custom range that runs past today.
    elapsed = max((min(period.end, periods.today()) - period.start).days + 1, 0)
    return (limit * months * elapsed / full_days).quantize(Decimal("0.01"))


def _fmt(kind, value) -> str:
    value = Decimal(value)
    if kind == "currency":
        sign, value = ("−" if value < 0 else ""), abs(value)
        return f"{sign}${value:,.0f}" if value >= 1000 else f"{sign}${value:,.2f}"
    if kind == "percent":
        return f"{value:.1f}%"
    if kind in ("rate", "decimal"):
        return f"{value:.2f}"
    return f"{value:,.0f}"


def evaluate(metrics, period, comps, rules=None) -> list[dict]:
    """Every active rule with its result for this period (hit True/False, or None when there's no data)."""
    key = ("alerts", period.start, period.end)
    cached = rules is None
    if cached and key in metrics._cache:
        return metrics._cache[key]
    if metrics.division:
        # Limits are set for the whole company; a division's numbers would trip them all the time.
        return []
    rules = list(AlertRule.objects.filter(is_active=True)) if rules is None else rules
    out = []
    for rule in rules:
        meta = KPIS.get(rule.metric)
        other_meta = KPIS.get(rule.compare_metric) if rule.condition in AlertRule.VS_METRIC else None
        if not meta or (meta["source"] == "expenses" and not metrics.finance_visible):
            continue
        if rule.condition in AlertRule.VS_METRIC and (not other_meta or (other_meta["source"] == "expenses" and not metrics.finance_visible)):
            continue
        result = {
            "id": rule.id,
            "metric": rule.metric,
            "label": meta["label"],
            "kind": meta["kind"],
            "condition": rule.condition,
            "threshold": rule.threshold,
            "compare_to": rule.compare_to,
            "compare_metric": rule.compare_metric,
            "compare_label": other_meta["label"] if other_meta else "",
            "note": rule.note,
            "is_active": rule.is_active,
            "hit": None,
            "value": None,
            "message": "",
        }
        value = metrics.value(rule.metric, period)
        if value is None:
            result["message"] = "No data for this period."
            out.append(result)
            continue
        value = Decimal(value)
        result["value"] = value
        if other_meta:
            other_value = metrics.value(rule.compare_metric, period)
            if other_value is None:
                result["message"] = f"No data for {other_meta['label']} in this period."
                out.append(result)
                continue
            other_value = Decimal(other_value)
            hit = value > other_value if rule.condition == AlertRule.ABOVE_METRIC else value < other_value
            word = "higher than" if rule.condition == AlertRule.ABOVE_METRIC else "lower than"
            result.update(
                hit=hit,
                limit=other_value,
                message=f"{meta['label']} is {_fmt(meta['kind'], value)}, {word if hit else 'not ' + word} {other_meta['label']} ({_fmt(other_meta['kind'], other_value)}).",
            )
        elif rule.condition in (AlertRule.ABOVE, AlertRule.BELOW):
            limit = Decimal(rule.threshold)
            scaled = ""
            if additive(rule.metric) and limit:
                limit = monthly_limit(limit, period)
                if limit != rule.threshold:
                    scaled = f" (your monthly limit {_fmt(meta['kind'], rule.threshold)} scaled to this period)"
            hit = value > limit if rule.condition == AlertRule.ABOVE else value < limit
            word = "above" if rule.condition == AlertRule.ABOVE else "below"
            result.update(
                hit=hit,
                limit=limit,
                message=f"{meta['label']} is {_fmt(meta['kind'], value)}, {word if hit else 'not ' + word} {_fmt(meta['kind'], limit)}{scaled}.",
            )
        else:
            other = comps.get(rule.compare_to)
            before = metrics.value(rule.metric, other) if other else None
            if before is None or Decimal(before) == 0:
                result["message"] = "Nothing to compare with."
                out.append(result)
                continue
            change = ((value - Decimal(before)) / abs(Decimal(before)) * 100).quantize(Decimal("0.1"))
            rising = rule.condition == AlertRule.UP_PCT
            hit = change >= rule.threshold if rising else change <= -rule.threshold
            against = "the previous period" if rule.compare_to == "previous" else "the same period last year"
            result.update(
                hit=hit,
                change=change,
                message=f"{meta['label']} is {_fmt(meta['kind'], value)}, {'+' if change > 0 else ''}{change}% vs {against} ({_fmt(meta['kind'], before)}).",
            )
        out.append(result)
    if cached:
        metrics._cache[key] = out
    return out


def triggered(metrics, period, comps) -> dict:
    """{metric: [messages]} for alerts that fired."""
    hits = {}
    for row in evaluate(metrics, period, comps):
        if row["hit"]:
            hits.setdefault(row["metric"], []).append(row["message"])
    return hits
