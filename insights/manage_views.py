"""Master Dashboard settings: alert limits, line-item rules, and the old MRR tab merge."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Max, Sum
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from analytics.mrr_models import MrrBooking
from insights.alerts import evaluate
from insights.metrics import KPIS
from insights.models import AlertRule, InsightsSetting, RecurringPlan, STATUS_CANCELLED
from insights.mrr_merge import _index, suggestions
from insights.views import _body, _context, _integration, _no_integration, _period_payload, error, respond
from operations.models import DIVISION_CHOICES, REVENUE_CATEGORY_CHOICES, InvoiceLineItem, LineItemRule
from operations.revenue import classify_lines, clear_rule_cache, match_line

CONDITIONS = dict(AlertRule.CONDITION_CHOICES)
DIVISIONS = dict(DIVISION_CHOICES)
CATEGORIES = dict(REVENUE_CATEGORY_CHOICES)
RECLASSIFY_KEY = "reclassify_status"
RULES_CHANGED_KEY = "line_item_rules_changed_at"


def _mark_changed():
    clear_rule_cache()
    InsightsSetting.put(RULES_CHANGED_KEY, {"at": timezone.now().isoformat()})


# ——— alerts ———
def _alert_input(rule: AlertRule, data: dict, user=None) -> AlertRule:
    if "metric" in data or rule.pk is None:
        if data.get("metric") not in KPIS or KPIS[data["metric"]]["note"]:
            raise ValueError("Choose a number to watch.")
        from financials.access import can_see_finances

        if KPIS[data["metric"]]["source"] == "expenses" and not can_see_finances(user):
            raise ValueError("Alerts on financial numbers need Financials access.")
        rule.metric = data["metric"]
    if "condition" in data or rule.pk is None:
        if data.get("condition") not in CONDITIONS:
            raise ValueError("Choose a condition.")
        rule.condition = data["condition"]
    if rule.condition in AlertRule.VS_METRIC:
        other_key = data.get("compare_metric") or rule.compare_metric
        other = KPIS.get(other_key)
        if not other or other["note"] or other_key == rule.metric:
            raise ValueError("Choose the other number to compare with.")
        if other["kind"] != KPIS[rule.metric]["kind"]:
            raise ValueError("Pick two numbers of the same type, like two dollar amounts.")
        from financials.access import can_see_finances

        if other["source"] == "expenses" and not can_see_finances(user):
            raise ValueError("Alerts on financial numbers need Financials access.")
        rule.compare_metric = other_key
        rule.threshold = Decimal(0)
    elif "threshold" in data or rule.pk is None:
        rule.compare_metric = ""
        try:
            rule.threshold = Decimal(str(data.get("threshold")).replace(",", "").replace("$", "").replace("%", "").strip()).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise ValueError("Enter a number for the limit.")
        if rule.condition in (AlertRule.UP_PCT, AlertRule.DOWN_PCT) and rule.threshold <= 0:
            raise ValueError("Enter a percentage above 0.")
    if "compare_to" in data:
        rule.compare_to = data["compare_to"] if data["compare_to"] in ("previous", "last_year") else "previous"
    if "note" in data:
        rule.note = str(data["note"] or "")[:255]
    if "is_active" in data:
        rule.is_active = bool(data["is_active"])
    return rule


@csrf_exempt
@require_http_methods(["GET", "POST"])
def alerts(request):
    if request.method == "POST":
        if not (request.user.is_staff or request.user.is_superuser):
            return error("Only managers can set alerts.", status=403)
        rule = AlertRule(created_by=request.user)
        try:
            _alert_input(rule, _body(request), request.user)
        except ValueError as exc:
            return error(str(exc))
        rule.save()
        return respond({"id": rule.id})
    metrics, period, comps = _context(request)
    if not metrics:
        return _no_integration()
    division_note = "Alerts are checked for the whole company, so the division filter doesn't apply here." if metrics.division else ""
    metrics.division = ""
    rules = list(AlertRule.objects.all())
    return respond(
        {
            **_period_payload(period, comps),
            "division_note": division_note,
            "rules": evaluate(metrics, period, comps, rules=rules),
            "metrics": [
                {"key": k, "label": m["label"], "kind": m["kind"], "point_in_time": m["point_in_time"]}
                for k, m in KPIS.items()
                if not m["note"] and (metrics.finance_visible or m["source"] != "expenses")
            ],
            "conditions": [{"key": k, "label": v} for k, v in AlertRule.CONDITION_CHOICES],
        }
    )


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def alert_detail(request, pk):
    rule = AlertRule.objects.filter(pk=pk).first()
    if not rule:
        return error("Alert not found.", status=404)
    if request.method == "DELETE":
        rule.delete()
        return respond({"deleted": pk})
    try:
        _alert_input(rule, _body(request), request.user)
    except ValueError as exc:
        return error(str(exc))
    rule.save()
    return respond({"id": rule.id})


# ——— line-item rules ———
def rule_row(rule: LineItemRule) -> dict:
    return {
        "id": rule.id,
        "keyword": rule.keyword,
        "division": rule.division,
        "division_label": DIVISIONS.get(rule.division, "Same as the main service"),
        "revenue_category": rule.revenue_category,
        "category_label": CATEGORIES.get(rule.revenue_category, "Depends on the job (recurring or not)"),
        "priority": rule.priority,
        "active": rule.active,
        "updated_at": rule.updated_at,
    }


def _rule_input(rule: LineItemRule, data: dict) -> LineItemRule:
    if "keyword" in data or rule.pk is None:
        keyword = str(data.get("keyword") or "").strip()[:120]
        if len(keyword) < 2:
            raise ValueError("Enter a word or phrase from the line item name.")
        rule.keyword = keyword
    if "division" in data:
        if data["division"] and data["division"] not in DIVISIONS:
            raise ValueError("Unknown division.")
        rule.division = data["division"] or ""
    if "revenue_category" in data:
        if data["revenue_category"] and data["revenue_category"] not in CATEGORIES:
            raise ValueError("Unknown revenue type.")
        rule.revenue_category = data["revenue_category"] or ""
    if "priority" in data:
        try:
            rule.priority = int(data["priority"])
        except (TypeError, ValueError):
            raise ValueError("Priority must be a whole number.")
    if "active" in data:
        rule.active = bool(data["active"])
    return rule


STALE_AFTER = timedelta(minutes=30)


def _reclassify_status():
    status = InsightsSetting.get(RECLASSIFY_KEY, {}) or {}
    # A worker that died mid-run would leave "running" forever: after 30 minutes, offer the button again.
    since = status.get("started_at") if status.get("state") == "running" else status.get("queued_at")
    if status.get("state") in ("queued", "running") and since:
        from datetime import datetime

        if timezone.now() - datetime.fromisoformat(since) > STALE_AFTER:
            status = {**status, "state": "failed", "error": "The last recalculation didn't finish."}
    changed = (InsightsSetting.get(RULES_CHANGED_KEY, {}) or {}).get("at")
    applied = status.get("started_at") or status.get("finished_at")
    status["pending_changes"] = bool(changed and (not applied or changed > applied))
    return status


@csrf_exempt
@require_http_methods(["GET", "POST"])
def rules(request):
    if request.method == "POST":
        rule = LineItemRule()
        try:
            _rule_input(rule, _body(request))
        except ValueError as exc:
            return error(str(exc))
        rule.save()
        _mark_changed()
        return respond({"rule": rule_row(rule)})
    return respond(
        {
            "rules": [rule_row(r) for r in LineItemRule.objects.order_by("priority", "keyword")],
            "divisions": [{"key": k, "label": v} for k, v in DIVISION_CHOICES],
            "categories": [{"key": k, "label": v} for k, v in REVENUE_CATEGORY_CHOICES],
            "status": _reclassify_status(),
        }
    )


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def rule_detail(request, pk):
    rule = LineItemRule.objects.filter(pk=pk).first()
    if not rule:
        return error("Rule not found.", status=404)
    if request.method == "DELETE":
        rule.delete()
        _mark_changed()
        return respond({"deleted": pk})
    try:
        _rule_input(rule, _body(request))
    except ValueError as exc:
        return error(str(exc))
    rule.save()
    _mark_changed()
    return respond({"rule": rule_row(rule)})


@csrf_exempt
@require_http_methods(["POST"])
def rule_test(request):
    """How one line item name would be classified (with the rules as they are now)."""
    data = _body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        return error("Enter a line item name.")
    clear_rule_cache()
    matched = match_line(name)
    rule = None
    if matched:
        from operations.revenue import normalize

        text = normalize(name)
        for r in LineItemRule.objects.filter(active=True).order_by("priority", "id"):
            import re

            if re.search(rf"(?<![a-z]){re.escape(normalize(r.keyword))}(?![a-z])", text):
                rule = rule_row(r)
                break
    results = {}
    for recurring in (True, False):
        division, category, _ = classify_lines([(name, Decimal(100))], recurring=recurring, is_company=bool(data.get("company")))
        results["recurring" if recurring else "one_off"] = {"division": DIVISIONS.get(division, division), "category": CATEGORIES.get(category, category)}
    return respond({"name": name, "rule": rule, "results": results})


@require_GET
def rule_lines(request):
    """Line item names billed in the last 24 months, with how they're classified; unmatched first."""
    integration = _integration()
    if not integration:
        return _no_integration()
    clear_rule_cache()
    since = timezone.now() - timedelta(days=730)
    rows = (
        InvoiceLineItem.objects.filter(invoice__integration=integration, invoice__issued_date__gte=since)
        .exclude(invoice__invoice_status="draft")
        .exclude(invoice__client__is_internal=True)
        .values("name")
        .annotate(n=Count("id"), revenue=Sum("net_total"), division=Max("division"), category=Max("revenue_category"))
    )
    search = (request.GET.get("q") or "").strip().lower()
    only_unmatched = request.GET.get("unmatched") == "1"
    out = []
    for row in rows:
        name = row["name"] or ""
        if search and search not in name.lower():
            continue
        matched = match_line(name) is not None
        if only_unmatched and matched:
            continue
        out.append(
            {
                "name": name,
                "count": row["n"],
                "revenue": row["revenue"] or Decimal(0),
                "division": DIVISIONS.get(row["division"], row["division"] or "—"),
                "category": CATEGORIES.get(row["category"], row["category"] or "—"),
                "matched": matched,
            }
        )
    out.sort(key=lambda r: (r["matched"], -r["revenue"]))
    return respond({"lines": out[:300], "total": len(out)})


@csrf_exempt
@require_http_methods(["POST"])
def rules_apply(request):
    """Recalculate revenue (and everything built on it) with the current rules, in the background."""
    status = _reclassify_status()
    if status.get("state") in ("queued", "running"):
        return respond({"status": status})
    from insights.tasks import rebuild_insights_task

    InsightsSetting.put(RECLASSIFY_KEY, {**status, "state": "queued", "queued_at": timezone.now().isoformat(), "error": ""})
    try:
        rebuild_insights_task.delay()
    except Exception as exc:
        InsightsSetting.put(RECLASSIFY_KEY, {**status, "state": "failed", "error": f"Couldn't start: {exc}"[:300]})
        return error("The background worker isn't reachable. The nightly sync will apply the rules.", status=503)
    return respond({"status": _reclassify_status()})


# ——— old MRR tab ———
def _plan_brief(plan: RecurringPlan) -> dict:
    return {
        "id": plan.id,
        "name": plan.name,
        "status": plan.status,
        "monthly_value": plan.effective_monthly_value,
        "frequency": plan.frequency,
        "start_date": plan.start_date,
    }


@require_GET
def old_tab(request):
    integration = _integration()
    if not integration:
        return _no_integration()
    _, plans = _index(integration)
    bookings = list(MrrBooking.objects.select_related("plan", "plan__client").order_by("name"))
    active = [b for b in bookings if b.status == MrrBooking.STATUS_ACTIVE]

    def total(items):
        return sum((b.monthly_value or Decimal(0) for b in items), Decimal(0))

    linked_active = [b for b in active if b.plan_id]

    def jobber_value(plan):
        return plan.effective_monthly_value if plan.status != STATUS_CANCELLED else Decimal(0)

    # Several bookings can describe one Jobber plan (e.g. a re-booked client): compare per plan, not per booking.
    by_plan = {}
    for b in linked_active:
        by_plan.setdefault(b.plan_id, []).append(b)
    differences = []
    for group in by_plan.values():
        plan, old = group[0].plan, total(group)
        diff = jobber_value(plan) - old
        if abs(diff) >= Decimal(20) and abs(diff) >= abs(old) * Decimal("0.1"):
            differences.append(
                {
                    "booking": group[0].name + (f" ({len(group)} bookings)" if len(group) > 1 else ""),
                    "old_tab": old,
                    "jobber": jobber_value(plan),
                    "plan": _plan_brief(plan),
                    "booking_id": group[0].id,
                }
            )
    needs_review = [b for b in bookings if not b.plan_id and b.plan_match != MrrBooking.MATCH_IGNORED]
    return respond(
        {
            "summary": {
                "bookings": len(bookings),
                "linked": sum(1 for b in bookings if b.plan_id),
                "ignored": sum(1 for b in bookings if b.plan_match == MrrBooking.MATCH_IGNORED),
                "needs_review": len(needs_review),
                "old_active_mrr": total(active),
                "old_active_linked_mrr": total(linked_active),
                "jobber_mrr_of_linked": sum((jobber_value(group[0].plan) for group in by_plan.values()), Decimal(0)),
                "shared_plans": sum(1 for group in by_plan.values() if len(group) > 1),
                "old_active_unlinked_mrr": total([b for b in active if not b.plan_id and b.plan_match != MrrBooking.MATCH_IGNORED]),
            },
            "needs_review": [
                {
                    "id": b.id,
                    "name": b.name,
                    "status": b.status,
                    "monthly_value": b.monthly_value,
                    "frequency": b.frequency,
                    "start_date": b.start_date,
                    "end_date": b.end_date,
                    "suggestions": [_plan_brief(p) for p in suggestions(b, plans)],
                }
                for b in needs_review
            ],
            "differences": sorted(differences, key=lambda d: -abs(d["jobber"] - d["old_tab"])),
        }
    )


@csrf_exempt
@require_http_methods(["PATCH"])
def old_tab_link(request, pk):
    booking = MrrBooking.objects.filter(pk=pk).first()
    if not booking:
        return error("Booking not found.", status=404)
    data = _body(request)
    if data.get("ignore"):
        booking.plan, booking.plan_match = None, MrrBooking.MATCH_IGNORED
    elif data.get("plan_id"):
        plan = RecurringPlan.objects.filter(pk=data["plan_id"], integration=_integration()).first()
        if not plan:
            return error("Plan not found.", status=404)
        booking.plan, booking.plan_match = plan, MrrBooking.MATCH_MANUAL
    else:  # back to automatic matching (applied by the next nightly sync)
        booking.plan, booking.plan_match = None, ""
    booking.save(update_fields=["plan", "plan_match", "updated_at"])
    return respond({"id": booking.id, "plan_match": booking.plan_match})


@require_GET
def plan_search(request):
    integration = _integration()
    q = (request.GET.get("q") or "").strip()
    if not integration or len(q) < 2:
        return respond({"plans": []})
    plans = RecurringPlan.objects.filter(integration=integration, name__icontains=q).order_by("name")[:15]
    return respond({"plans": [_plan_brief(p) for p in plans]})
