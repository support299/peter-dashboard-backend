"""Division + revenue category classification from Jobber line items.

Job titles look like "Client – City – Service", so title keywords miss most
commercial work. Invoice line item names ("Dental Clinic Cleaning",
"Bi-Weekly Cleaning – Detailed", "Equipment Rental (1 Day)") describe the
service, so revenue is classified per line item:

1. Each line is matched against LineItemRule keywords (editable in Admin).
2. The invoice's main service is the largest matched line.
3. Unmatched lines (add-ons such as "Inside oven") inherit the main service.
4. Tips / fees take category "other" but keep the main division.
5. Company clients buying residential-type services count as Commercial.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from django.db import transaction
from django.db.models import Prefetch

from operations.models import (
    REVENUE_COMMERCIAL,
    REVENUE_FIRST_DEEP,
    REVENUE_MOVE,
    REVENUE_OTHER,
    REVENUE_RENTALS,
    REVENUE_RESIDENTIAL_OTHER,
    REVENUE_RESIDENTIAL_RECURRING,
    REVENUE_SPECIALIZED,
    Client,
    Invoice,
    InvoiceLineItem,
    Job,
    JobLineItem,
    LineItemRule,
    Visit,
)

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")

# (keyword, division, category, priority). Empty division = inherit from the
# invoice's main service; empty category = decide from context (recurring job?).
DEFAULT_LINE_ITEM_RULES = [
    ("equipment rental", "rentals", REVENUE_RENTALS, 1),
    ("rental", "rentals", REVENUE_RENTALS, 2),
    ("tip", "", REVENUE_OTHER, 5),
    ("tips", "", REVENUE_OTHER, 5),
    ("cancellation fee", "", REVENUE_OTHER, 6),
    ("late payment fee", "", REVENUE_OTHER, 6),
    ("fee", "", REVENUE_OTHER, 7),
    ("deposit", "", REVENUE_OTHER, 7),
    ("dental", "commercial", REVENUE_COMMERCIAL, 10),
    ("clinic", "commercial", REVENUE_COMMERCIAL, 11),
    ("office", "commercial", REVENUE_COMMERCIAL, 12),
    ("bureau", "commercial", REVENUE_COMMERCIAL, 13),
    ("bureaux", "commercial", REVENUE_COMMERCIAL, 13),
    ("workspace", "commercial", REVENUE_COMMERCIAL, 14),
    ("commercial", "commercial", REVENUE_COMMERCIAL, 15),
    ("apartment building", "commercial", REVENUE_COMMERCIAL, 16),
    ("condo", "commercial", REVENUE_COMMERCIAL, 17),
    ("business", "commercial", REVENUE_COMMERCIAL, 18),
    ("restaurant", "commercial", REVENUE_COMMERCIAL, 19),
    ("store", "commercial", REVENUE_COMMERCIAL, 19),
    ("gym", "commercial", REVENUE_COMMERCIAL, 19),
    ("daycare", "commercial", REVENUE_COMMERCIAL, 19),
    ("warehouse", "commercial", REVENUE_COMMERCIAL, 19),
    ("window", "specialized", REVENUE_SPECIALIZED, 30),
    ("windows", "specialized", REVENUE_SPECIALIZED, 30),
    ("carpet", "specialized", REVENUE_SPECIALIZED, 31),
    ("upholstery", "specialized", REVENUE_SPECIALIZED, 32),
    ("sofa", "specialized", REVENUE_SPECIALIZED, 33),
    ("rug", "specialized", REVENUE_SPECIALIZED, 34),
    ("mattress", "specialized", REVENUE_SPECIALIZED, 35),
    ("pressure wash", "specialized", REVENUE_SPECIALIZED, 36),
    ("pressure washing", "specialized", REVENUE_SPECIALIZED, 36),
    ("soft wash", "specialized", REVENUE_SPECIALIZED, 37),
    ("gutter", "specialized", REVENUE_SPECIALIZED, 38),
    ("post renovation", "specialized", REVENUE_SPECIALIZED, 40),
    ("post-renovation", "specialized", REVENUE_SPECIALIZED, 40),
    ("post construction", "specialized", REVENUE_SPECIALIZED, 41),
    ("post-construction", "specialized", REVENUE_SPECIALIZED, 41),
    ("floor", "specialized", REVENUE_SPECIALIZED, 42),
    ("move in", "residential", REVENUE_MOVE, 50),
    ("move-in", "residential", REVENUE_MOVE, 50),
    ("move out", "residential", REVENUE_MOVE, 50),
    ("move-out", "residential", REVENUE_MOVE, 50),
    ("moving", "residential", REVENUE_MOVE, 51),
    ("demenagement", "residential", REVENUE_MOVE, 51),
    ("listing cleaning", "residential", REVENUE_MOVE, 52),
    ("deep clean", "residential", REVENUE_FIRST_DEEP, 60),
    ("deep cleaning", "residential", REVENUE_FIRST_DEEP, 60),
    ("first clean", "residential", REVENUE_FIRST_DEEP, 61),
    ("first cleaning", "residential", REVENUE_FIRST_DEEP, 61),
    ("first time", "residential", REVENUE_FIRST_DEEP, 61),
    ("initial clean", "residential", REVENUE_FIRST_DEEP, 61),
    ("premier menage", "residential", REVENUE_FIRST_DEEP, 62),
    ("grand menage", "residential", REVENUE_FIRST_DEEP, 62),
    ("spring cleaning", "residential", REVENUE_FIRST_DEEP, 63),
    ("weekly", "residential", REVENUE_RESIDENTIAL_RECURRING, 70),
    ("bi-weekly", "residential", REVENUE_RESIDENTIAL_RECURRING, 70),
    ("bi weekly", "residential", REVENUE_RESIDENTIAL_RECURRING, 70),
    ("biweekly", "residential", REVENUE_RESIDENTIAL_RECURRING, 70),
    ("monthly", "residential", REVENUE_RESIDENTIAL_RECURRING, 70),
    ("bi-monthly", "residential", REVENUE_RESIDENTIAL_RECURRING, 70),
    ("maid service", "residential", REVENUE_RESIDENTIAL_RECURRING, 71),
    ("mensuel", "residential", REVENUE_RESIDENTIAL_RECURRING, 72),
    ("aux 2 semaines", "residential", REVENUE_RESIDENTIAL_RECURRING, 72),
    ("aux 2 semaine", "residential", REVENUE_RESIDENTIAL_RECURRING, 72),
    ("aux semaines", "residential", REVENUE_RESIDENTIAL_RECURRING, 72),
    ("aux 4 semaines", "residential", REVENUE_RESIDENTIAL_RECURRING, 72),
    ("occasional", "residential", REVENUE_RESIDENTIAL_OTHER, 80),
    ("occassional", "residential", REVENUE_RESIDENTIAL_OTHER, 80),
    ("air bnb", "residential", REVENUE_RESIDENTIAL_OTHER, 81),
    ("airbnb", "residential", REVENUE_RESIDENTIAL_OTHER, 81),
    ("residential", "residential", "", 90),
    ("cleaning service", "residential", "", 91),
    ("menage", "residential", "", 92),
    ("maintenance cleaning", "residential", "", 93),
    ("cleaning", "residential", "", 99),
]

# Categories that a company client's purchase turns into Commercial.
RESIDENTIAL_TYPE_CATEGORIES = {"", REVENUE_RESIDENTIAL_RECURRING, REVENUE_RESIDENTIAL_OTHER}


def seed_line_item_rules():
    if LineItemRule.objects.exists():
        return
    LineItemRule.objects.bulk_create(
        [
            LineItemRule(keyword=k, division=d, revenue_category=c, priority=p, active=True)
            for k, d, c, p in DEFAULT_LINE_ITEM_RULES
        ]
    )
    clear_rule_cache()


def normalize(text: str | None) -> str:
    """Lowercase, strip accents and punctuation noise so 'Ménage' == 'menage'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("�", "e")
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=1)
def _rules():
    try:
        rows = list(
            LineItemRule.objects.filter(active=True)
            .order_by("priority", "id")
            .values_list("keyword", "division", "revenue_category")
        )
    except Exception:
        rows = []
    if not rows:
        rows = [(k, d, c) for k, d, c, _ in sorted(DEFAULT_LINE_ITEM_RULES, key=lambda r: r[3])]
    return [(re.compile(rf"(?<![a-z]){re.escape(normalize(k))}(?![a-z])"), d, c) for k, d, c in rows]


def clear_rule_cache():
    _rules.cache_clear()


def match_line(name: str | None):
    """Return (division, category) for a line item name, or None when no rule matches."""
    text = normalize(name)
    if not text:
        return None
    for pattern, division, category in _rules():
        if pattern.search(text):
            return division, category
    return None


def _context_category(category: str, *, recurring: bool) -> str:
    if category:
        return category
    return REVENUE_RESIDENTIAL_RECURRING if recurring else REVENUE_RESIDENTIAL_OTHER


def _apply_company(division: str, category: str, *, is_company: bool):
    if is_company and division == "residential" and category in RESIDENTIAL_TYPE_CATEGORIES:
        return "commercial", REVENUE_COMMERCIAL
    return division, category


def classify_lines(lines, *, recurring: bool, is_company: bool, fallback_division: str = ""):
    """
    lines: iterable of (name, total). Returns (main_division, main_category, [(division, category), ...]).
    """
    lines = list(lines)
    matched = [match_line(name) for name, _ in lines]

    # Main service = largest line with a specific category; generic "cleaning"
    # lines only decide when nothing specific was billed.
    main = None
    for strong in (True, False):
        best = None
        for (name, total), hit in zip(lines, matched):
            if not hit or not hit[0] or hit[1] in (REVENUE_OTHER, REVENUE_RENTALS):
                continue
            if bool(hit[1]) != strong:
                continue
            amount = abs(total or Decimal("0"))
            if best is None or amount > best:
                best, main = amount, hit
        if main is not None:
            break
    if main is None:
        rental = next((hit for hit in matched if hit and hit[1] == REVENUE_RENTALS), None)
        main = rental or (fallback_division or ("commercial" if is_company else "residential"), "")
    main_division, main_category = main
    main_category = _context_category(main_category, recurring=recurring) if main_division == "residential" else (
        main_category or main_division
    )
    main_division, main_category = _apply_company(main_division, main_category, is_company=is_company)
    if main_division in {"commercial", "specialized", "rentals"} and main_category in {"", main_division}:
        main_category = {"commercial": REVENUE_COMMERCIAL, "specialized": REVENUE_SPECIALIZED, "rentals": REVENUE_RENTALS}[
            main_division
        ]

    results = []
    for hit in matched:
        if not hit:
            results.append((main_division, main_category))
            continue
        division, category = hit
        if not category and division in ("", "residential"):
            # Generic line ("Interior oven cleaning") follows the main service.
            results.append((main_division, main_category))
            continue
        if category == REVENUE_OTHER:
            results.append((main_division, REVENUE_OTHER))
            continue
        if category == REVENUE_RENTALS:
            results.append(("rentals", REVENUE_RENTALS))
            continue
        division = division or main_division
        if division == "residential":
            category = _context_category(category, recurring=recurring)
        division, category = _apply_company(division, category, is_company=is_company)
        if division == "commercial":
            category = REVENUE_COMMERCIAL
        results.append((division, category))
    return main_division, main_category, results


def _discount_ratio(invoice: Invoice) -> Decimal:
    subtotal = invoice.subtotal or Decimal("0")
    discount = invoice.discount_amount or Decimal("0")
    if subtotal <= 0 or discount <= 0:
        return Decimal("1")
    return max(Decimal("0"), (subtotal - discount) / subtotal)


def classify_invoice(invoice: Invoice, *, save=True):
    """Classify one invoice's line items. Expects line_items and jobs prefetched for bulk use."""
    jobs = list(invoice.jobs.all())
    recurring = any(job.is_recurring for job in jobs)
    is_company = bool(invoice.client and invoice.client.is_company)
    fallback = next((job.division for job in jobs if job.division), "")
    lines = list(invoice.line_items.all())
    main_division, _, results = classify_lines(
        [(line.name, line.total) for line in lines],
        recurring=recurring,
        is_company=is_company,
        fallback_division=fallback,
    )
    ratio = _discount_ratio(invoice)
    for line, (division, category) in zip(lines, results):
        line.division = division
        line.revenue_category = category
        line.net_total = ((line.total or Decimal("0")) * ratio).quantize(CENTS, rounding=ROUND_HALF_UP)
    invoice.division = main_division
    subtotal = invoice.subtotal if invoice.subtotal is not None else sum((line.total or 0) for line in lines)
    invoice.net_revenue = (Decimal(subtotal or 0) - (invoice.discount_amount or Decimal("0"))).quantize(CENTS)
    if save:
        InvoiceLineItem.objects.bulk_update(lines, ["division", "revenue_category", "net_total"])
        invoice.save(update_fields=["division", "net_revenue"])
    return lines


def reclassify_all(integration) -> dict:
    """Classify every invoice, then derive Job / Visit / Client divisions from billed services."""
    seed_line_item_rules()
    clear_rule_cache()
    invoices = (
        Invoice.objects.filter(integration=integration)
        .select_related("client")
        .prefetch_related("line_items", Prefetch("jobs", queryset=Job.objects.only("id", "is_recurring", "division")))
    )
    line_updates, invoice_updates = [], []
    job_amounts = defaultdict(lambda: defaultdict(Decimal))
    client_amounts = defaultdict(lambda: defaultdict(Decimal))
    for invoice in invoices.iterator(chunk_size=500):
        lines = classify_invoice(invoice, save=False)
        line_updates.extend(lines)
        invoice_updates.append(invoice)
        for line in lines:
            amount = abs(line.net_total or Decimal("0"))
            for job in invoice.jobs.all():
                job_amounts[job.id][line.division] += amount
            if invoice.client_id:
                client_amounts[invoice.client_id][line.division] += amount

    with transaction.atomic():
        InvoiceLineItem.objects.bulk_update(line_updates, ["division", "revenue_category", "net_total"], batch_size=1000)
        Invoice.objects.bulk_update(invoice_updates, ["division", "net_revenue"], batch_size=1000)

        # Jobs: billed division wins; un-invoiced jobs fall back to their own line items.
        jobs = Job.objects.filter(integration=integration).select_related("client").prefetch_related(
            Prefetch("line_items", queryset=JobLineItem.objects.only("id", "job_id", "name", "total"))
        )
        job_updates = []
        for job in jobs.iterator(chunk_size=500):
            amounts = job_amounts.get(job.id)
            if amounts:
                division = max(amounts.items(), key=lambda kv: kv[1])[0]
            else:
                items = [(item.name, item.total) for item in job.line_items.all()]
                if not items or not any(match_line(name) for name, _ in items):
                    continue
                division, _, _ = classify_lines(
                    items,
                    recurring=job.is_recurring,
                    is_company=bool(job.client and job.client.is_company),
                    fallback_division=job.division,
                )
            if division and division != job.division:
                job.division = division
                job_updates.append(job)
        Job.objects.bulk_update(job_updates, ["division"], batch_size=1000)

        visit_updates = 0
        for division in {"residential", "commercial", "specialized", "rentals"}:
            visit_updates += (
                Visit.objects.filter(integration=integration, job__division=division)
                .exclude(division=division)
                .update(division=division)
            )

        client_updates = []
        for client in Client.objects.filter(integration=integration, id__in=list(client_amounts)).only("id", "division"):
            division = max(client_amounts[client.id].items(), key=lambda kv: kv[1])[0]
            if division and division != client.division:
                client.division = division
                client_updates.append(client)
        Client.objects.bulk_update(client_updates, ["division"], batch_size=1000)

    return {
        "invoices": len(invoice_updates),
        "line_items": len(line_updates),
        "jobs_changed": len(job_updates),
        "visits_changed": visit_updates,
        "clients_changed": len(client_updates),
    }
