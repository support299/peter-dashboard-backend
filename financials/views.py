"""Financials API (/api/financials/)."""

from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.urls import path
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from accounts.authz import protect
from analytics.hub_models import HubContractorPay
from financials import contractor_pay, importer, ledger, recurring, storage
from financials.access import finance_role, has_role, require_role
from financials.models import (
    ACCOUNT_BANK,
    ACCOUNT_KIND_CHOICES,
    DEBT_KINDS,
    DIVISION_SHARED,
    EXPENSE_DIVISION_CHOICES,
    FREQUENCY_CHOICES,
    GROUP_CHOICES,
    GROUP_OTHER,
    PAYMENT_METHOD_CHOICES,
    REVIEW_CHECKS,
    ROLE_ADMIN,
    ROLE_CHOICES,
    ROLE_ENTRY,
    ROLE_FULL,
    SOURCE_CHOICES,
    SOURCE_HUB,
    SOURCE_MANUAL,
    SOURCE_PAYROLL,
    STATUS_CHOICES,
    STATUS_UNPAID,
    BalanceEntry,
    Category,
    Expense,
    FinanceAccess,
    FinancialAccount,
    ImportBatch,
    MonthlyTotal,
    MonthReview,
    PayRun,
    RecurringExpense,
)
from insights import periods
from insights.views import _context, _no_integration, _period_payload, error, respond
from operations.models import DIVISION_CHOICES

ENTRY, FULL, ADMIN = ROLE_ENTRY, ROLE_FULL, ROLE_ADMIN
DIVISIONS = dict(EXPENSE_DIVISION_CHOICES)
METHODS = dict(PAYMENT_METHOD_CHOICES)
SOURCES = dict(SOURCE_CHOICES)
GROUPS = dict(GROUP_CHOICES)
STATUSES = dict(STATUS_CHOICES)
FREQUENCIES = dict(FREQUENCY_CHOICES)
ACCOUNT_KINDS = dict(ACCOUNT_KIND_CHOICES)
CHECK_KEYS = [key for key, _ in REVIEW_CHECKS]
LOCKED_MESSAGE = "{month} is marked reviewed. Reopen it in Month review to make changes."


# ——— helpers ———
def _body(request) -> dict:
    if request.content_type and request.content_type.startswith("multipart/"):
        return request.POST.dict()
    try:
        return json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return {}


def _date(value, field="date", required=False):
    if value in (None, ""):
        if required:
            raise ValueError(f"Enter the {field}.")
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"Invalid {field}: {value}")


def _money(value, field, required=False, default=Decimal(0)):
    if value in (None, ""):
        if required:
            raise ValueError(f"Enter the {field}.")
        return default
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"Invalid {field}: {value}")


def _month(value) -> date:
    """'2026-10' or '2026-10-01' → date(2026, 10, 1)."""
    text = str(value or "")[:7]
    try:
        year, month = (int(x) for x in text.split("-"))
        return date(year, month, 1)
    except ValueError:
        raise ValueError("Invalid month.")


def _choice(value, choices: dict, field, default=""):
    if value in (None, ""):
        return default
    if value not in choices:
        raise ValueError(f"Unknown {field}.")
    return value


def _locked_error(day):
    return error(LOCKED_MESSAGE.format(month=f"{day:%B %Y}"), status=409)


def _user_name(user):
    if not user:
        return ""
    return user.get_full_name() or user.email or user.username


def _period(request):
    period = periods.from_request(request)
    return period, periods.comparisons(period)


def _category(value, *, field="category", parent=None):
    if value in (None, ""):
        return None
    cat = Category.objects.filter(pk=value).select_related("parent").first()
    if not cat:
        raise ValueError(f"Unknown {field}.")
    if parent is None and cat.parent_id:
        raise ValueError("Choose a category, then a subcategory.")
    if parent is not None and cat.parent_id != parent.id:
        raise ValueError(f"{cat.name} is not a subcategory of {parent.name}.")
    return cat


# ——— serializers ———
def category_row(c: Category) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "group": c.group,
        "parent_id": c.parent_id,
        "counts_as_payroll": c.counts_as_payroll,
        "counts_as_labour": c.counts_as_labour,
        "in_profit_and_loss": c.in_profit_and_loss,
        "system_key": c.system_key,
        "is_active": c.is_active,
        "sort_order": c.sort_order,
    }


def expense_row(e: Expense) -> dict:
    return {
        "id": e.id,
        "date": e.date,
        "vendor": e.vendor,
        "description": e.description,
        "amount": e.amount,
        "gst": e.gst,
        "qst": e.qst,
        "total": e.total,
        "category_id": e.category_id,
        "category": e.category.name,
        "subcategory_id": e.subcategory_id,
        "subcategory": e.subcategory.name if e.subcategory_id else "",
        "group": e.category.group,
        "division": e.division,
        "division_label": DIVISIONS.get(e.division, e.division),
        "payment_method": e.payment_method,
        "payment_label": METHODS.get(e.payment_method, ""),
        "status": e.status,
        "notes": e.notes,
        "source": e.source,
        "source_label": SOURCES.get(e.source, e.source),
        "has_receipt": e.has_receipt,
        "receipt_name": e.receipt_name,
        "receipt_type": e.receipt_type,
        "pending_verification": e.pending_verification,
        "due_date": e.due_date,
        "recurring_id": e.recurring_id,
        "pay_run_id": e.pay_run_id,
        "import_batch_id": e.import_batch_id,
        "automatic": e.source == SOURCE_HUB,
        "contractor_pay_warning": contractor_pay.expense_warning(e),
        "created_by": _user_name(e.created_by),
        "created_at": e.created_at,
        "updated_at": e.updated_at,
    }


def payrun_row(run: PayRun) -> dict:
    return {
        "id": run.id,
        "pay_date": run.pay_date,
        "period_start": run.period_start,
        "period_end": run.period_end,
        "cleaning_amount": run.cleaning_amount,
        "office_amount": run.office_amount,
        "total": run.total,
        "notes": run.notes,
        "has_receipt": run.has_receipt,
        "receipt_name": run.receipt_name,
        "created_by": _user_name(run.created_by),
        "created_at": run.created_at,
    }


def recurring_row(rec: RecurringExpense) -> dict:
    today = timezone.localdate()
    upcoming = [d for d in recurring.due_dates(rec, today + timedelta(days=400)) if d >= today]
    return {
        "id": rec.id,
        "name": rec.name,
        "vendor": rec.vendor,
        "description": rec.description,
        "amount": rec.amount,
        "gst": rec.gst,
        "qst": rec.qst,
        "total": rec.total,
        "category_id": rec.category_id,
        "category": rec.category.name,
        "subcategory_id": rec.subcategory_id,
        "subcategory": rec.subcategory.name if rec.subcategory_id else "",
        "division": rec.division,
        "division_label": DIVISIONS.get(rec.division, rec.division),
        "payment_method": rec.payment_method,
        "frequency": rec.frequency,
        "frequency_label": FREQUENCIES.get(rec.frequency, rec.frequency),
        "start_date": rec.start_date,
        "end_date": rec.end_date,
        "is_active": rec.is_active,
        "notes": rec.notes,
        "next_due": upcoming[0] if rec.is_active and upcoming else None,
        "pending": rec.expenses.filter(pending_verification=True).count(),
    }


def account_row(acc: FinancialAccount, as_of: date | None = None, compare: dict | None = None) -> dict:
    balances = acc.balances.all()
    latest = (balances.filter(date__lte=as_of) if as_of else balances).order_by("-date").first()
    row = {
        "id": acc.id,
        "name": acc.name,
        "kind": acc.kind,
        "kind_label": ACCOUNT_KINDS.get(acc.kind, acc.kind),
        "is_debt": acc.is_debt,
        "institution": acc.institution,
        "credit_limit": acc.credit_limit,
        "interest_rate": acc.interest_rate,
        "is_active": acc.is_active,
        "notes": acc.notes,
        "balance": latest.balance if latest else None,
        "balance_date": latest.date if latest else None,
        "history": [{"id": b.id, "date": b.date, "balance": b.balance, "notes": b.notes} for b in balances.order_by("-date")[:24]],
    }
    for name, day in (compare or {}).items():
        prior = balances.filter(date__lte=day).order_by("-date").first()
        row[name] = prior.balance if prior else None
    return row


# ——— meta ———
@require_GET
def meta(request):
    role = finance_role(request.user)
    if not role:
        return respond({"role": ""})
    cats = Category.objects.all() if role == ADMIN else Category.objects.filter(is_active=True)
    return respond(
        {
            "role": role,
            "storage_configured": storage.configured(),
            "groups": [{"key": k, "label": v} for k, v in GROUP_CHOICES],
            "categories": [category_row(c) for c in cats.order_by("group", "parent_id", "sort_order", "name")],
            "divisions": [{"key": k, "label": v} for k, v in EXPENSE_DIVISION_CHOICES],
            "payment_methods": [{"key": k, "label": v} for k, v in PAYMENT_METHOD_CHOICES],
            "statuses": [{"key": k, "label": v} for k, v in STATUS_CHOICES],
            "frequencies": [{"key": k, "label": v} for k, v in FREQUENCY_CHOICES],
            "account_kinds": [{"key": k, "label": v} for k, v in ACCOUNT_KIND_CHOICES],
            "review_checks": [{"key": k, "label": v} for k, v in REVIEW_CHECKS],
            "roles": [{"key": k, "label": v} for k, v in ROLE_CHOICES],
            "import_columns": importer.TEMPLATE_HEADERS,
            "today": timezone.localdate(),
        }
    )


# ——— expenses ———
def _expense_qs(request):
    qs = Expense.objects.select_related("category", "subcategory", "created_by")
    if not has_role(request.user, FULL):
        # Entry-only users see and correct their own expenses (never payroll).
        qs = qs.filter(created_by=request.user, pay_run__isnull=True)
    return qs


def _filtered(request, qs):
    g = request.GET
    if g.get("period") or g.get("from"):
        period = periods.from_request(request)
        qs = qs.filter(date__gte=period.start, date__lte=period.end)
    search = (g.get("q") or "").strip()
    if search:
        qs = qs.filter(Q(vendor__icontains=search) | Q(description__icontains=search) | Q(notes__icontains=search))
    if g.get("category"):
        qs = qs.filter(Q(category_id=g["category"]) | Q(subcategory_id=g["category"]))
    if g.get("group"):
        qs = qs.filter(category__group=g["group"])
    if g.get("division"):
        qs = qs.filter(division=g["division"])
    if g.get("status"):
        qs = qs.filter(status=g["status"])
    if g.get("source"):
        qs = qs.filter(source=g["source"])
    if g.get("pending") in ("1", "0"):
        qs = qs.filter(pending_verification=g["pending"] == "1")
    if g.get("receipt") == "missing":
        qs = qs.filter(receipt_key="")
    elif g.get("receipt") == "attached":
        qs = qs.exclude(receipt_key="")
    if g.get("batch"):
        qs = qs.filter(import_batch_id=g["batch"])
    return qs


def apply_expense(expense: Expense, data: dict) -> Expense:
    """Validate and set fields from the request (partial updates allowed)."""
    new = expense.pk is None
    if new or "date" in data:
        expense.date = _date(data.get("date"), required=True)
    if new or "amount" in data:
        expense.amount = _money(data.get("amount"), "amount before tax", required=True)
    for field in ("gst", "qst"):
        if new or field in data:
            setattr(expense, field, _money(data.get(field), field.upper()))
    if new or "category_id" in data:
        category = _category(data.get("category_id"))
        if not category:
            raise ValueError("Choose a category.")
        if new and not category.is_active:
            raise ValueError("This category is no longer used.")
        expense.category = category
    if "subcategory_id" in data:
        expense.subcategory = _category(data.get("subcategory_id"), field="subcategory", parent=expense.category)
    elif new or (expense.subcategory_id and expense.subcategory.parent_id != expense.category_id):
        expense.subcategory = None
    for field in ("vendor", "description"):
        if field in data:
            setattr(expense, field, str(data[field] or "").strip()[: 200 if field == "vendor" else 500])
    if "notes" in data:
        expense.notes = str(data["notes"] or "")[:5000]
    if new or "division" in data:
        expense.division = _choice(data.get("division"), DIVISIONS, "division", DIVISION_SHARED)
    if "payment_method" in data:
        expense.payment_method = _choice(data.get("payment_method"), METHODS, "payment method")
    if "status" in data:
        expense.status = _choice(data.get("status"), STATUSES, "status", expense.status)
    if not (expense.vendor or expense.description):
        raise ValueError("Enter a vendor or a description.")
    return expense


def likely_duplicates(expense: Expense) -> list:
    """Same total within 3 days (or a pending recurring charge), excluding itself."""
    qs = Expense.objects.filter(
        date__gte=expense.date - timedelta(days=3), date__lte=expense.date + timedelta(days=3), total=expense.amount + expense.gst + expense.qst
    ).select_related("category", "subcategory", "created_by")
    if expense.pk:
        qs = qs.exclude(pk=expense.pk)
    if expense.vendor:
        qs = qs.filter(Q(vendor__iexact=expense.vendor) | Q(vendor="") | Q(category=expense.category))
    return [expense_row(e) for e in qs[:5]]


def _attach(record, request, prefix):
    upload = request.FILES.get("receipt")
    if not upload:
        return None
    fields = storage.save(upload, prefix=prefix)
    old = record.receipt_key
    for key, value in fields.items():
        setattr(record, key, value)
    return old


@csrf_exempt
@require_http_methods(["GET", "POST"])
def expenses(request):
    if request.method == "POST":
        data = _body(request)
        expense = Expense(source=SOURCE_MANUAL, created_by=request.user, updated_by=request.user)
        try:
            apply_expense(expense, data)
        except ValueError as exc:
            return error(str(exc))
        if ledger.is_locked(expense.date):
            return _locked_error(expense.date)
        if str(data.get("confirm_duplicate", "")).lower() not in ("1", "true"):
            dupes = likely_duplicates(expense)
            if dupes and not has_role(request.user, FULL):
                dupes = [{"id": d["id"], "date": d["date"], "total": d["total"], "category": d["category"], "vendor": "", "description": "An expense"} for d in dupes]
            warning = contractor_pay.expense_warning(expense)
            if dupes or warning:
                return respond(
                    {"ok": False, "error": warning or "This looks like an expense that's already entered.", "duplicates": dupes, "contractor_pay_warning": warning},
                    status=409,
                )
        try:
            _attach(expense, request, "receipts")
        except storage.ReceiptError as exc:
            return error(str(exc))
        expense.save()
        return respond({"expense": expense_row(expense)})

    qs = _filtered(request, _expense_qs(request))
    page = Paginator(qs.order_by("-date", "-id"), min(int(request.GET.get("page_size") or 50), 200)).get_page(request.GET.get("page") or 1)
    period, comps = _period(request)
    payload = {
        **_period_payload(period, comps),
        "count": page.paginator.count,
        "page": page.number,
        "pages": page.paginator.num_pages,
        "results": [expense_row(e) for e in page.object_list],
    }
    if has_role(request.user, FULL):
        sums = qs.filter(pending_verification=False).aggregate(amount=Sum("amount"), gst=Sum("gst"), qst=Sum("qst"), total=Sum("total"))
        payload["sums"] = {k: v or Decimal(0) for k, v in sums.items()}
        payload["pending_count"] = Expense.objects.filter(pending_verification=True, date__lte=timezone.localdate()).count()
    return respond(payload)


def _own_expense(request, pk):
    return _expense_qs(request).filter(pk=pk).first()


@csrf_exempt
@require_http_methods(["GET", "PATCH", "POST", "DELETE"])
def expense_detail(request, pk):
    expense = _own_expense(request, pk)
    if not expense:
        return error("Expense not found.", status=404)
    if request.method == "GET":
        return respond({"expense": expense_row(expense)})
    if expense.pay_run_id:
        return error("This expense comes from a pay run. Edit the pay run instead.", status=409)
    if expense.source == SOURCE_HUB:
        return error("This expense is added automatically from contractor pay in the Hub. Change the pay in the Hub.", status=409)
    if ledger.is_locked(expense.date):
        return _locked_error(expense.date)
    if request.method == "DELETE":
        storage.delete(expense.receipt_key)
        expense.delete()
        return respond({"deleted": pk})
    # PATCH (JSON) or POST (multipart, to send a new receipt with the edits).
    data = _body(request)
    try:
        apply_expense(expense, data)
    except ValueError as exc:
        return error(str(exc))
    if ledger.is_locked(expense.date):
        return _locked_error(expense.date)
    try:
        old_key = _attach(expense, request, "receipts")
    except storage.ReceiptError as exc:
        return error(str(exc))
    expense.updated_by = request.user
    expense.save()
    if old_key:
        storage.delete(old_key)
    return respond({"expense": expense_row(expense)})


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
def expense_receipt(request, pk):
    expense = _own_expense(request, pk)
    if not expense:
        return error("Expense not found.", status=404)
    return _receipt(request, expense, "receipts")


def _receipt(request, record, prefix):
    if request.method == "GET":
        if not record.receipt_key:
            return error("No receipt attached.", status=404)
        return respond({"url": storage.link(record.receipt_key, record.receipt_name, download=request.GET.get("download") == "1")})
    day = getattr(record, "date", None) or getattr(record, "pay_date", None)
    if ledger.is_locked(day):
        return _locked_error(day)
    if request.method == "DELETE":
        storage.delete(record.receipt_key)
        for key, value in storage.EMPTY.items():
            setattr(record, key, value)
        record.save(update_fields=[*storage.EMPTY.keys(), "updated_at"])
        return respond({"removed": True})
    try:
        old_key = _attach(record, request, prefix)
    except storage.ReceiptError as exc:
        return error(str(exc))
    if old_key is None:
        return error("Choose a file to attach.")
    record.save(update_fields=[*storage.EMPTY.keys(), "updated_at"])
    if old_key:
        storage.delete(old_key)
    return respond({"receipt_name": record.receipt_name})


@csrf_exempt
@require_http_methods(["POST"])
def expense_verify(request, pk):
    """Confirm a recurring charge (optionally correcting it) so it counts in totals."""
    expense = Expense.objects.filter(pk=pk, pending_verification=True).select_related("category", "subcategory").first()
    if not expense:
        return error("Nothing to verify.", status=404)
    data = _body(request)
    try:
        apply_expense(expense, data)
    except ValueError as exc:
        return error(str(exc))
    if ledger.is_locked(expense.date):
        return _locked_error(expense.date)
    expense.pending_verification = False
    expense.updated_by = request.user
    expense.save()
    return respond({"expense": expense_row(expense)})


@csrf_exempt
@require_http_methods(["POST"])
def expense_skip(request, pk):
    """'Not charged this time': remove the scheduled entry and remember not to create it again."""
    expense = Expense.objects.filter(pk=pk, pending_verification=True).select_related("recurring").first()
    if not expense:
        return error("Nothing to skip.", status=404)
    rec = expense.recurring
    if rec and expense.due_date:
        rec.skipped_dates = sorted({*(rec.skipped_dates or []), expense.due_date.isoformat()})
        rec.save(update_fields=["skipped_dates", "updated_at"])
    expense.delete()
    return respond({"skipped": pk})


@require_GET
def expenses_export(request):
    qs = _filtered(request, _expense_qs(request)).order_by("date", "id")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="expenses.csv"'
    response.write("﻿")  # Excel opens UTF-8 correctly with a BOM
    writer = csv.writer(response)
    writer.writerow([*importer.TEMPLATE_HEADERS, "Group", "Source", "Receipt", "Verified"])
    for e in qs:
        writer.writerow(
            [
                e.date.isoformat(),
                e.vendor,
                e.description,
                e.amount,
                e.gst,
                e.qst,
                e.total,
                e.category.name,
                e.subcategory.name if e.subcategory_id else "",
                DIVISIONS.get(e.division, e.division),
                METHODS.get(e.payment_method, ""),
                STATUSES.get(e.status, e.status),
                e.notes,
                GROUPS.get(e.category.group, ""),
                SOURCES.get(e.source, e.source),
                "Yes" if e.has_receipt else "No",
                "No" if e.pending_verification else "Yes",
            ]
        )
    return response


# ——— import ———
@csrf_exempt
@require_http_methods(["POST"])
def import_expenses(request):
    upload = request.FILES.get("file")
    if not upload:
        return error("Choose a CSV or Excel file.")
    if not upload.name.lower().endswith((".csv", ".txt", ".xlsx", ".xlsm")):
        return error("Upload a .csv or .xlsx file.")
    data = request.POST
    try:
        default = _category(data.get("default_category")) if data.get("default_category") else None
        parsed = importer.parse(upload, order="mdy" if data.get("date_format") == "mdy" else "dmy", default_category=default)
    except ValueError as exc:
        return error(str(exc))
    except Exception:
        return error("This file couldn't be read. Save it as CSV (UTF-8) or .xlsx and try again.")
    rows = parsed["rows"]
    summary = {
        "rows": len(rows),
        "ready": sum(1 for r in rows if not r["errors"] and not r["duplicate"]),
        "duplicates": sum(1 for r in rows if not r["errors"] and r["duplicate"]),
        "errors": sum(1 for r in rows if r["errors"]),
        "columns": parsed["columns"],
        "taxes_missing": parsed["taxes_missing"],
    }
    if data.get("commit") not in ("1", "true"):
        return respond({"summary": summary, "rows": [importer.preview_row(r) for r in rows[:1000]]})
    with transaction.atomic():
        batch = importer.commit(parsed, file_name=upload.name, user=request.user, include_duplicates=data.get("include_duplicates") in ("1", "true"))
    return respond({"summary": summary, "batch": {"id": batch.id, "imported": batch.rows_imported, "skipped": batch.rows_skipped}})


@require_GET
def import_batches(request):
    qs = ImportBatch.objects.annotate(remaining=Count("expenses")).select_related("created_by")[:50]
    return respond(
        {
            "batches": [
                {
                    "id": b.id,
                    "file_name": b.file_name,
                    "imported": b.rows_imported,
                    "skipped": b.rows_skipped,
                    "remaining": b.remaining,
                    "created_by": _user_name(b.created_by),
                    "created_at": b.created_at,
                }
                for b in qs
            ]
        }
    )


@csrf_exempt
@require_http_methods(["DELETE"])
def import_batch_undo(request, pk):
    batch = ImportBatch.objects.filter(pk=pk).first()
    if not batch:
        return error("Import not found.", status=404)
    locked = [e.date for e in batch.expenses.all() if ledger.is_locked(e.date)]
    if locked:
        return _locked_error(locked[0])
    removed = batch.expenses.count()
    for key in batch.expenses.exclude(receipt_key="").values_list("receipt_key", flat=True):
        storage.delete(key)
    batch.expenses.all().delete()
    batch.delete()
    return respond({"removed": removed})


# ——— categories ———
@csrf_exempt
@require_http_methods(["GET", "POST"])
def categories(request):
    if request.method == "GET":
        return respond({"categories": [category_row(c) for c in Category.objects.all()]})
    if not has_role(request.user, ADMIN):
        return error("Only Financials admins can change categories.", status=403)
    data = _body(request)
    try:
        parent = _category(data.get("parent_id")) if data.get("parent_id") else None
        name = str(data.get("name") or "").strip()[:120]
        if not name:
            raise ValueError("Enter a name.")
        group = parent.group if parent else _choice(data.get("group"), GROUPS, "group")
        if not group:
            raise ValueError("Choose a group.")
        if Category.objects.filter(parent=parent, name__iexact=name, group=group).exists():
            raise ValueError("That category already exists.")
    except ValueError as exc:
        return error(str(exc))
    cat = Category.objects.create(
        group=group,
        name=name,
        parent=parent,
        counts_as_payroll=bool(data.get("counts_as_payroll")),
        counts_as_labour=bool(data.get("counts_as_labour")),
        in_profit_and_loss=bool(data.get("in_profit_and_loss", group != GROUP_OTHER)),
        sort_order=(Category.objects.filter(parent=parent, group=group).count() + 1) * 10,
    )
    return respond({"category": category_row(cat)})


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def category_detail(request, pk):
    if not has_role(request.user, ADMIN):
        return error("Only Financials admins can change categories.", status=403)
    cat = Category.objects.filter(pk=pk).first()
    if not cat:
        return error("Category not found.", status=404)
    if request.method == "DELETE":
        used = (
            Expense.objects.filter(Q(category=cat) | Q(subcategory=cat)).exists()
            or MonthlyTotal.objects.filter(category=cat).exists()
            or RecurringExpense.objects.filter(Q(category=cat) | Q(subcategory=cat)).exists()
            or cat.children.exists()
        )
        if used or cat.system_key:
            cat.is_active = False
            cat.save(update_fields=["is_active", "updated_at"])
            return respond({"category": category_row(cat), "message": "It's used by existing records, so it was hidden instead of deleted."})
        cat.delete()
        return respond({"deleted": pk})
    data = _body(request)
    if "name" in data:
        name = str(data["name"] or "").strip()[:120]
        if not name:
            return error("Enter a name.")
        cat.name = name
    if "group" in data and not cat.parent_id:
        try:
            cat.group = _choice(data["group"], GROUPS, "group", cat.group)
        except ValueError as exc:
            return error(str(exc))
    for field in ("counts_as_payroll", "counts_as_labour", "in_profit_and_loss", "is_active"):
        if field in data:
            setattr(cat, field, bool(data[field]))
    if "sort_order" in data:
        cat.sort_order = int(data["sort_order"] or 0)
    cat.save()
    if not cat.parent_id:
        cat.children.exclude(group=cat.group).update(group=cat.group)
    return respond({"category": category_row(cat)})


# ——— payroll ———
def _payroll_category(key):
    return Category.objects.filter(system_key=key).first()


def sync_payrun_expenses(run: PayRun, user):
    """A pay run is two expenses: cleaning staff (COGS) and office (operating expenses)."""
    label = f"Pay run {run.period_start:%b %d}–{run.period_end:%b %d, %Y}" if run.period_start and run.period_end else "Pay run"
    for key, amount, who in (("payroll_cleaning", run.cleaning_amount, "cleaning staff"), ("payroll_office", run.office_amount, "office")):
        category = _payroll_category(key)
        existing = run.expenses.filter(category=category).first() if category else None
        if not category or not amount:
            if existing:
                existing.delete()
            continue
        expense = existing or Expense(pay_run=run, category=category, source=SOURCE_PAYROLL, created_by=user)
        expense.date = run.pay_date
        expense.vendor = "PayEvo"
        expense.description = f"{label} · {who}"
        expense.amount, expense.gst, expense.qst = amount, Decimal(0), Decimal(0)
        expense.division = DIVISION_SHARED
        expense.payment_method = "payroll"
        expense.notes = run.notes
        expense.updated_by = user
        expense.save()


def apply_payrun(run: PayRun, data: dict) -> PayRun:
    new = run.pk is None
    if new or "pay_date" in data:
        run.pay_date = _date(data.get("pay_date"), "pay date", required=True)
    for field in ("period_start", "period_end"):
        if field in data:
            setattr(run, field, _date(data.get(field), field.replace("_", " ")))
    for field, label in (("cleaning_amount", "cleaning staff payroll"), ("office_amount", "office payroll")):
        if new or field in data:
            setattr(run, field, _money(data.get(field), label))
    if "notes" in data:
        run.notes = str(data["notes"] or "")[:5000]
    if not run.cleaning_amount and not run.office_amount:
        raise ValueError("Enter at least one payroll amount.")
    if run.period_start and run.period_end and run.period_end < run.period_start:
        raise ValueError("The pay period ends before it starts.")
    return run


@csrf_exempt
@require_http_methods(["GET", "POST"])
def payroll(request):
    if request.method == "POST":
        data = _body(request)
        run = PayRun(created_by=request.user, updated_by=request.user)
        try:
            apply_payrun(run, data)
        except ValueError as exc:
            return error(str(exc))
        if ledger.is_locked(run.pay_date):
            return _locked_error(run.pay_date)
        if PayRun.objects.filter(pay_date=run.pay_date).exists() and str(data.get("confirm_duplicate", "")).lower() not in ("1", "true"):
            return respond({"ok": False, "error": f"A pay run on {run.pay_date:%b %d, %Y} is already entered.", "duplicates": []}, status=409)
        try:
            _attach(run, request, "payroll")
        except storage.ReceiptError as exc:
            return error(str(exc))
        with transaction.atomic():
            run.save()
            sync_payrun_expenses(run, request.user)
        return respond({"pay_run": payrun_row(run)})
    period, comps = _period(request)
    qs = PayRun.objects.select_related("created_by")
    if request.GET.get("period") or request.GET.get("from"):
        qs = qs.filter(pay_date__gte=period.start, pay_date__lte=period.end)
    runs = list(qs[:200])
    return respond(
        {
            **_period_payload(period, comps),
            "results": [payrun_row(r) for r in runs],
            "sums": {
                "cleaning": sum((r.cleaning_amount for r in runs), Decimal(0)),
                "office": sum((r.office_amount for r in runs), Decimal(0)),
                "total": sum((r.total for r in runs), Decimal(0)),
            },
        }
    )


@csrf_exempt
@require_http_methods(["PATCH", "POST", "DELETE"])
def payroll_detail(request, pk):
    run = PayRun.objects.filter(pk=pk).first()
    if not run:
        return error("Pay run not found.", status=404)
    if ledger.is_locked(run.pay_date):
        return _locked_error(run.pay_date)
    if request.method == "DELETE":
        storage.delete(run.receipt_key)
        run.delete()
        return respond({"deleted": pk})
    data = _body(request)
    try:
        apply_payrun(run, data)
    except ValueError as exc:
        return error(str(exc))
    if ledger.is_locked(run.pay_date):
        return _locked_error(run.pay_date)
    try:
        old_key = _attach(run, request, "payroll")
    except storage.ReceiptError as exc:
        return error(str(exc))
    run.updated_by = request.user
    with transaction.atomic():
        run.save()
        sync_payrun_expenses(run, request.user)
    if old_key:
        storage.delete(old_key)
    return respond({"pay_run": payrun_row(run)})


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
def payroll_receipt(request, pk):
    run = PayRun.objects.filter(pk=pk).first()
    if not run:
        return error("Pay run not found.", status=404)
    return _receipt(request, run, "payroll")


# ——— recurring ———
def apply_recurring(rec: RecurringExpense, data: dict) -> RecurringExpense:
    new = rec.pk is None
    if new or "name" in data:
        rec.name = str(data.get("name") or "").strip()[:200]
        if not rec.name:
            raise ValueError("Enter a name (e.g. Office rent).")
    if new or "amount" in data:
        rec.amount = _money(data.get("amount"), "amount before tax", required=True)
    for field in ("gst", "qst"):
        if new or field in data:
            setattr(rec, field, _money(data.get(field), field.upper()))
    if new or "category_id" in data:
        rec.category = _category(data.get("category_id"))
        if not rec.category:
            raise ValueError("Choose a category.")
    if new or "subcategory_id" in data or "category_id" in data:
        rec.subcategory = _category(data.get("subcategory_id"), field="subcategory", parent=rec.category)
    for field, limit in (("vendor", 200), ("description", 500), ("notes", 5000)):
        if field in data:
            setattr(rec, field, str(data[field] or "").strip()[:limit])
    if new or "division" in data:
        rec.division = _choice(data.get("division"), DIVISIONS, "division", DIVISION_SHARED)
    if "payment_method" in data:
        rec.payment_method = _choice(data.get("payment_method"), METHODS, "payment method")
    if new or "frequency" in data:
        rec.frequency = _choice(data.get("frequency"), FREQUENCIES, "frequency", "monthly")
    if new or "start_date" in data:
        rec.start_date = _date(data.get("start_date"), "start date", required=True)
    if "end_date" in data:
        rec.end_date = _date(data.get("end_date"), "end date")
    if "is_active" in data:
        rec.is_active = bool(data["is_active"])
    if rec.end_date and rec.end_date < rec.start_date:
        raise ValueError("The end date is before the start date.")
    return rec


@csrf_exempt
@require_http_methods(["GET", "POST"])
def recurring_list(request):
    if request.method == "POST":
        rec = RecurringExpense(created_by=request.user)
        try:
            apply_recurring(rec, _body(request))
        except ValueError as exc:
            return error(str(exc))
        rec.save()
        recurring.generate(rec)
        return respond({"recurring": recurring_row(rec)})
    pending = Expense.objects.filter(pending_verification=True).select_related("category", "subcategory", "created_by").order_by("date", "id")
    return respond(
        {
            "results": [recurring_row(r) for r in RecurringExpense.objects.select_related("category", "subcategory")],
            "pending": [expense_row(e) for e in pending[:200]],
        }
    )


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def recurring_detail(request, pk):
    rec = RecurringExpense.objects.filter(pk=pk).first()
    if not rec:
        return error("Recurring expense not found.", status=404)
    if request.method == "DELETE":
        rec.expenses.filter(pending_verification=True).delete()
        rec.delete()  # verified expenses stay (their link is cleared)
        return respond({"deleted": pk})
    try:
        apply_recurring(rec, _body(request))
    except ValueError as exc:
        return error(str(exc))
    rec.save()
    recurring.sync_pending(rec)
    recurring.generate(rec)
    return respond({"recurring": recurring_row(rec)})


# ——— monthly totals ———
def total_row(t: MonthlyTotal, individual: Decimal) -> dict:
    return {
        "id": t.id,
        "month": t.month,
        "category_id": t.category_id,
        "category": t.category.name,
        "group": t.category.group,
        "division": t.division,
        "division_label": DIVISIONS.get(t.division, t.division),
        "amount": t.amount,
        "notes": t.notes,
        "individual": individual,
        "difference": t.amount - individual,
        "contractor_pay_warning": contractor_pay.monthly_total_warning(t),
    }


def reconciliation(month: date) -> dict:
    """Per category: individual expenses vs the monthly total, and which one reports use."""
    end = ledger._month_end(month)
    individual = {
        row["category_id"]: row["s"] or Decimal(0)
        for row in Expense.objects.filter(date__gte=month, date__lte=end, pending_verification=False)
        .values("category_id")
        .annotate(s=Sum("amount"))
        .order_by()
    }
    totals = list(MonthlyTotal.objects.filter(month=month).select_related("category"))
    by_cat = {}
    for t in totals:
        by_cat[t.category_id] = by_cat.get(t.category_id, Decimal(0)) + t.amount
    cats = ledger.categories()
    lines = []
    for cat_id in sorted(set(individual) | set(by_cat), key=lambda c: (cats[c].group, cats[c].name)):
        ind, tot = individual.get(cat_id, Decimal(0)), by_cat.get(cat_id)
        lines.append(
            {
                "category_id": cat_id,
                "category": cats[cat_id].name,
                "group": cats[cat_id].group,
                "individual": ind,
                "total": tot,
                "difference": (tot - ind) if tot is not None else None,
                "used": tot if tot is not None else ind,
                "uses": "total" if tot is not None else "individual",
            }
        )
    return {"totals": [total_row(t, individual.get(t.category_id, Decimal(0))) for t in totals], "lines": lines}


@csrf_exempt
@require_http_methods(["GET", "POST"])
def monthly_totals(request):
    if request.method == "GET":
        try:
            month = _month(request.GET.get("month") or timezone.localdate().isoformat())
        except ValueError as exc:
            return error(str(exc))
        return respond({"month": month, **reconciliation(month)})
    data = _body(request)
    try:
        month = _month(data.get("month"))
        category = _category(data.get("category_id"))
        if not category:
            raise ValueError("Choose a category.")
        division = _choice(data.get("division"), DIVISIONS, "division", DIVISION_SHARED)
        amount = _money(data.get("amount"), "amount", required=True)
    except ValueError as exc:
        return error(str(exc))
    if ledger.is_locked(month):
        return _locked_error(month)
    total, created = MonthlyTotal.objects.update_or_create(
        month=month, category=category, division=division, defaults={"amount": amount, "notes": str(data.get("notes") or "")[:5000], "created_by": request.user}
    )
    return respond({"created": created, "month": month, **reconciliation(month)})


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def monthly_total_detail(request, pk):
    total = MonthlyTotal.objects.filter(pk=pk).first()
    if not total:
        return error("Monthly total not found.", status=404)
    if ledger.is_locked(total.month):
        return _locked_error(total.month)
    month = total.month
    if request.method == "DELETE":
        total.delete()
    else:
        data = _body(request)
        try:
            if "amount" in data:
                total.amount = _money(data["amount"], "amount", required=True)
            if "notes" in data:
                total.notes = str(data["notes"] or "")[:5000]
        except ValueError as exc:
            return error(str(exc))
        total.save()
    return respond({"month": month, **reconciliation(month)})


# ——— dashboards: expenses, P&L, cash & debt, reports ———
def _finance_context(request):
    metrics, period, comps = _context(request)
    if metrics:
        metrics.division = ""  # Financials pages always show the whole company
        metrics.finance_visible = True
    return metrics, period, comps


def _has_expenses(metrics, period):
    return metrics._has("expenses", period)


def _category_rows(metrics, period, comps, group=None):
    cats = ledger.categories()
    current = ledger.by_category(period.start, period.end)
    others = {name: ledger.by_category(o.start, o.end) if _has_expenses(metrics, o) else None for name, o in comps.items()}
    keys = set(current) | {k for vals in others.values() if vals for k in vals}
    total = sum((v for k, v in current.items() if ledger.part_of(cats[k]) != "excluded"), Decimal(0))
    rows = []
    for cat_id in keys:
        cat = cats[cat_id]
        if group and cat.group != group:
            continue
        value = current.get(cat_id, Decimal(0))
        rows.append(
            {
                "key": cat_id,
                "label": cat.name,
                "group": cat.group,
                "part": ledger.part_of(cat),
                "current": value,
                "share": (value / total * 100).quantize(Decimal("0.1")) if total and ledger.part_of(cat) != "excluded" else None,
                **{name: (vals.get(cat_id, Decimal(0)) if vals is not None else None) for name, vals in others.items()},
            }
        )
    order = [g for g, _ in GROUP_CHOICES]
    return sorted(rows, key=lambda r: (order.index(r["group"]), -r["current"], r["label"]))


def _division_rows(metrics, period, comps):
    def amounts(p):
        return {d: ledger.total_expenses(parts) for d, parts in ledger.by_division(p.start, p.end).items()}

    current = amounts(period)
    others = {name: amounts(o) if _has_expenses(metrics, o) else None for name, o in comps.items()}
    return [
        {
            "key": key,
            "label": label,
            "current": current.get(key, Decimal(0)),
            **{name: (vals.get(key, Decimal(0)) if vals is not None else None) for name, vals in others.items()},
        }
        for key, label in EXPENSE_DIVISION_CHOICES
    ]


@require_GET
def summary(request):
    metrics, period, comps = _finance_context(request)
    if not metrics:
        return _no_integration()
    cards = [
        metrics.card(k, period, comps)
        for k in ("total_expenses", "cogs", "operating_expenses", "vehicle_expenses", "payroll", "cogs_pct", "opex_pct", "labour_cost_pct", "payroll_pct", "expenses_pct")
    ]
    pending = Expense.objects.filter(pending_verification=True, date__lte=timezone.localdate())
    parts = metrics.finance_parts(period)
    return respond(
        {
            **_period_payload(period, comps),
            "data_start": metrics.data_starts().get("expenses"),
            "cards": cards,
            "groups": [{"key": key, "label": label, "current": parts[ledger.GROUP_PART[key]]} for key, label in GROUP_CHOICES],
            "categories": _category_rows(metrics, period, comps),
            "divisions": _division_rows(metrics, period, comps),
            "pending": {"count": pending.count(), "total": pending.aggregate(s=Sum("amount"))["s"] or Decimal(0)},
            "unreviewed": [m for m in ledger.unreviewed_months(period.start, period.end) if m <= timezone.localdate()],
        }
    )


@require_GET
def contractor_pay_view(request):
    """What the Hub worked out for contractors in the period, and how much of it is in expenses."""
    period, comps = _period(request)
    start = contractor_pay.start_date()
    data = contractor_pay.summary(period.start, period.end)
    booked = Expense.objects.filter(source=SOURCE_HUB, date__gte=period.start, date__lte=period.end).aggregate(s=Sum("amount"))["s"]
    return respond(
        {
            **_period_payload(period, comps),
            **data,
            "lines": contractor_pay.lines(period.start, period.end, request.GET.get("contractor") or "") if request.GET.get("contractor") else [],
            "start_date": start,
            "booked": booked or Decimal(0),
            "has_data": HubContractorPay.objects.exists(),
        }
    )


PNL_LINES = [
    ("revenue", "Revenue", "total"),
    ("cogs", "Cost of sales (COGS)", "cost"),
    ("gross_profit", "Gross profit", "total"),
    ("gross_margin", "Gross margin", "ratio"),
    ("operating_expenses", "Operating expenses", "cost"),
    ("vehicle_expenses", "Vehicle expenses", "cost"),
    ("operating_profit", "Operating profit", "total"),
    ("operating_margin", "Operating margin", "ratio"),
    ("financial_costs", "Interest & financing costs", "cost"),
    ("net_profit", "Net profit", "total"),
    ("net_margin", "Net margin", "ratio"),
]
LINE_GROUP = {"cogs": "cogs", "operating_expenses": "opex", "vehicle_expenses": "vehicles", "financial_costs": "other"}


@require_GET
def pnl(request):
    metrics, period, comps = _finance_context(request)
    if not metrics:
        return _no_integration()
    cats = _category_rows(metrics, period, comps)
    lines = []
    for key, label, kind in PNL_LINES:
        card = metrics.card(key, period, comps)
        group = LINE_GROUP.get(key)
        lines.append(
            {
                **card,
                "label": label,
                "line": kind,
                "details": [c for c in cats if group and c["group"] == group and c["part"] != "excluded"],
            }
        )

    # By division: revenue and each division's direct costs; shared costs stay on their own line.
    revenue = metrics.revenue_by("division", period)
    costs = ledger.by_division(period.start, period.end)
    divisions = []
    for key, label in DIVISION_CHOICES:
        parts = costs.get(key, {p: Decimal(0) for p in ledger.PARTS})
        rev = revenue.get(key, Decimal(0))
        direct = ledger.total_expenses(parts)
        divisions.append(
            {
                "key": key,
                "label": label,
                "revenue": rev,
                "cogs": parts["cogs"],
                "gross_profit": rev - parts["cogs"],
                "gross_margin": ledger._pct(rev - parts["cogs"], rev),
                "other_costs": direct - parts["cogs"],
                "contribution": rev - direct,
            }
        )
    unassigned = sum((v for k, v in revenue.items() if k not in dict(DIVISION_CHOICES)), Decimal(0))
    if unassigned:
        divisions.append(
            {"key": "", "label": "Not assigned to a division", "revenue": unassigned, "cogs": Decimal(0), "gross_profit": unassigned,
             "gross_margin": None, "other_costs": Decimal(0), "contribution": unassigned}
        )
    covered = ledger.covers(period.start, period.end, metrics.data_starts().get("expenses"), "net_profit")
    if not covered:
        divisions = []
    shared = costs.get(DIVISION_SHARED, {p: Decimal(0) for p in ledger.PARTS})
    all_parts = ledger.summary(period.start, period.end)
    total_revenue = sum(revenue.values(), Decimal(0))
    company = {
        "revenue": total_revenue,
        "cogs": all_parts["cogs"],
        "gross_profit": total_revenue - all_parts["cogs"],
        "gross_margin": ledger._pct(total_revenue - all_parts["cogs"], total_revenue),
        "other_costs": ledger.total_expenses(all_parts) - all_parts["cogs"],
        # Equals net profit: every division's contribution minus shared costs.
        "contribution": total_revenue - ledger.total_expenses(all_parts),
    } if covered else None
    return respond(
        {
            **_period_payload(period, comps),
            "data_start": metrics.data_starts().get("expenses"),
            "lines": lines,
            "divisions": divisions,
            "company": company,
            "shared": None if not covered else {"cogs": shared["cogs"], "other_costs": ledger.total_expenses(shared) - shared["cogs"], "total": ledger.total_expenses(shared)},
            "excluded": [c for c in cats if c["part"] == "excluded"],
            "unreviewed": [m for m in ledger.unreviewed_months(period.start, period.end) if m <= timezone.localdate()],
        }
    )


@require_GET
def cash(request):
    period, comps = _period(request)
    accounts = [
        account_row(a, period.end, {**{name: o.end for name, o in comps.items()}, "opening": period.start - timedelta(days=1)})
        for a in FinancialAccount.objects.filter(is_active=True).prefetch_related("balances")
    ]

    def total(rows, debt, key="balance"):
        values = [r[key] for r in rows if r["is_debt"] == debt and r[key] is not None]
        return sum(values, Decimal(0)) if values else None

    totals = {}
    for key in ("balance", "opening", *comps.keys()):
        cash_total, debt_total = total(accounts, False, key), total(accounts, True, key)
        totals[key] = {
            "cash": cash_total,
            "debt": debt_total,
            "net": (cash_total or Decimal(0)) - (debt_total or Decimal(0)) if cash_total is not None or debt_total is not None else None,
        }
    opening_missing = sum(1 for r in accounts if not r["is_debt"] and r["balance"] is not None and r["opening"] is None)
    metrics, _, _ = _finance_context(request)
    other = _category_rows(metrics, period, comps, group=GROUP_OTHER) if metrics else []
    cash_flow = [metrics.card(k, period, comps) for k in ("cash_in", "cash_out", "net_cash_flow")] if metrics else []
    return respond(
        {
            **_period_payload(period, comps),
            "accounts": accounts,
            "inactive": [account_row(a) for a in FinancialAccount.objects.filter(is_active=False)],
            "totals": totals,
            "opening_missing": opening_missing,
            "transactions": other,
            "cash_flow": cash_flow,
        }
    )


def apply_account(acc: FinancialAccount, data: dict) -> FinancialAccount:
    new = acc.pk is None
    if new or "name" in data:
        acc.name = str(data.get("name") or "").strip()[:120]
        if not acc.name:
            raise ValueError("Enter a name.")
    if new or "kind" in data:
        acc.kind = _choice(data.get("kind"), ACCOUNT_KINDS, "account type", ACCOUNT_BANK)
    if "institution" in data:
        acc.institution = str(data["institution"] or "").strip()[:120]
    if "credit_limit" in data:
        acc.credit_limit = _money(data["credit_limit"], "credit limit", default=None)
    if "interest_rate" in data:
        acc.interest_rate = None if data["interest_rate"] in (None, "") else Decimal(str(data["interest_rate"])).quantize(Decimal("0.001"))
    if "notes" in data:
        acc.notes = str(data["notes"] or "")[:5000]
    if "is_active" in data:
        acc.is_active = bool(data["is_active"])
    return acc


@csrf_exempt
@require_http_methods(["POST"])
def accounts(request):
    data = _body(request)
    acc = FinancialAccount()
    try:
        apply_account(acc, data)
        balance = _money(data.get("balance"), "balance", default=None)
        balance_date = _date(data.get("balance_date"), "balance date") or timezone.localdate()
    except (ValueError, InvalidOperation) as exc:
        return error(str(exc) or "Invalid value.")
    with transaction.atomic():
        acc.save()
        if balance is not None:
            BalanceEntry.objects.create(account=acc, date=balance_date, balance=balance, created_by=request.user)
    return respond({"account": account_row(acc)})


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def account_detail(request, pk):
    acc = FinancialAccount.objects.filter(pk=pk).first()
    if not acc:
        return error("Account not found.", status=404)
    if request.method == "DELETE":
        if acc.balances.exists():
            acc.is_active = False
            acc.save(update_fields=["is_active", "updated_at"])
            return respond({"account": account_row(acc), "message": "It has balance history, so it was archived instead of deleted."})
        acc.delete()
        return respond({"deleted": pk})
    try:
        apply_account(acc, _body(request))
    except (ValueError, InvalidOperation) as exc:
        return error(str(exc) or "Invalid value.")
    acc.save()
    return respond({"account": account_row(acc)})


@csrf_exempt
@require_http_methods(["POST"])
def account_balance(request, pk):
    acc = FinancialAccount.objects.filter(pk=pk).first()
    if not acc:
        return error("Account not found.", status=404)
    data = _body(request)
    try:
        day = _date(data.get("date"), "date") or timezone.localdate()
        balance = _money(data.get("balance"), "balance", required=True)
    except ValueError as exc:
        return error(str(exc))
    BalanceEntry.objects.update_or_create(
        account=acc, date=day, defaults={"balance": balance, "notes": str(data.get("notes") or "")[:500], "created_by": request.user}
    )
    return respond({"account": account_row(acc)})


@csrf_exempt
@require_http_methods(["DELETE"])
def balance_detail(request, pk):
    entry = BalanceEntry.objects.filter(pk=pk).select_related("account").first()
    if not entry:
        return error("Balance not found.", status=404)
    account = entry.account
    entry.delete()
    return respond({"account": account_row(account)})


@require_GET
def report(request):
    """Monthly P&L for the selected period, with category detail.

    Each column covers the part of its month inside the period (the current month stops at
    today), and the Total column is the whole period, so every number matches Profit & Loss.
    """
    metrics, period, _ = _finance_context(request)
    if not metrics:
        return _no_integration()
    cats = ledger.categories()
    data_start = metrics.data_starts().get("expenses")

    def column(start, end, label, month=None):
        p = periods.Period("custom", start, end, end)
        has = ledger.covers(start, end, data_start, "cogs")
        revenue = metrics.value("revenue", p)
        parts = ledger.summary(start, end)
        values = {key: (ledger.kpi(key, parts, revenue) if ledger.covers(start, end, data_start, key) else None) for key, _, _ in PNL_LINES if key != "revenue"}
        values["revenue"] = revenue
        by_cat = ledger.by_category(start, end) if has else {}
        return {"month": month, "label": label, "has_expenses": has, "values": values, "categories": by_cat}

    columns = []
    for month in periods.months_between(period.start, period.end)[:36]:
        start, end = max(month, period.start), min(ledger._month_end(month), period.end)
        col = column(start, end, f"{month:%b %Y}", month)
        col["partial"] = start != month or end != ledger._month_end(month)
        columns.append(col)
    total = column(period.start, period.end, "Total")

    cat_ids = {cat_id for col in [*columns, total] for cat_id in col["categories"]}
    order = [g for g, _ in GROUP_CHOICES]
    categories_out = [
        {
            "id": cat_id,
            "label": cats[cat_id].name,
            "group": cats[cat_id].group,
            "part": ledger.part_of(cats[cat_id]),
            "values": {col["month"].isoformat(): (col["categories"].get(cat_id, Decimal(0)) if col["has_expenses"] else None) for col in columns},
            "total": total["categories"].get(cat_id, Decimal(0)) if total["has_expenses"] else None,
        }
        for cat_id in sorted(cat_ids, key=lambda c: (order.index(cats[c].group), cats[c].name))
    ]
    for col in [*columns, total]:
        col.pop("categories")
    return respond(
        {
            **_period_payload(period, periods.comparisons(period)),
            "lines": [{"key": k, "label": label, "line": kind} for k, label, kind in PNL_LINES],
            "columns": columns,
            "total": total,
            "categories": categories_out,
            "data_start": data_start,
        }
    )


# ——— month review ———
def review_hints(month: date) -> dict:
    end = ledger._month_end(month)
    expenses_qs = Expense.objects.filter(date__gte=month, date__lte=end)
    counted = expenses_qs.filter(pending_verification=False)
    balance_dates = {
        acc.id: acc.balances.filter(date__gte=month, date__lte=end).exists() for acc in FinancialAccount.objects.filter(is_active=True)
    }
    totals = reconciliation(month)["totals"]
    return {
        "expenses": counted.count(),
        "expenses_total": counted.aggregate(s=Sum("amount"))["s"] or Decimal(0),
        "missing_receipts": counted.filter(receipt_key="", pay_run__isnull=True).exclude(source=SOURCE_HUB).count(),
        "pending_recurring": expenses_qs.filter(pending_verification=True).count(),
        "unpaid": counted.filter(status=STATUS_UNPAID).count(),
        "pay_runs": PayRun.objects.filter(pay_date__gte=month, pay_date__lte=end).count(),
        "accounts_without_balance": [
            acc.name for acc in FinancialAccount.objects.filter(is_active=True, kind__in=[ACCOUNT_BANK, *DEBT_KINDS]) if not balance_dates.get(acc.id)
        ],
        "totals_with_difference": sum(1 for t in totals if t["difference"]),
    }


def review_row(review: MonthReview | None, month: date, hints=True) -> dict:
    row = {
        "month": month,
        "label": f"{month:%B %Y}",
        "status": review.status if review else MonthReview.STATUS_OPEN,
        "checks": {key: bool((review.checks if review else {}).get(key)) for key in CHECK_KEYS},
        "notes": review.notes if review else "",
        "reviewed_at": review.reviewed_at if review else None,
        "reviewed_by": _user_name(review.reviewed_by) if review else "",
    }
    if hints:
        row["hints"] = review_hints(month)
    return row


@require_GET
def reviews(request):
    today = timezone.localdate()
    months = [periods.add_months(periods.month_start(today), -i) for i in range(12)]
    found = {r.month: r for r in MonthReview.objects.filter(month__in=months).select_related("reviewed_by")}
    return respond({"months": [review_row(found.get(m), m, hints=False) for m in months]})


@csrf_exempt
@require_http_methods(["GET", "PATCH", "POST"])
def review_detail(request, month, action=""):
    try:
        month = _month(month)
    except ValueError as exc:
        return error(str(exc))
    review = MonthReview.objects.filter(month=month).select_related("reviewed_by").first()
    if request.method == "GET":
        return respond({"review": review_row(review, month)})
    review = review or MonthReview(month=month)
    if action == "reopen":
        if not has_role(request.user, ADMIN):
            return error("Only Financials admins can reopen a reviewed month.", status=403)
        review.status, review.reviewed_at, review.reviewed_by = MonthReview.STATUS_OPEN, None, None
        review.save()
        return respond({"review": review_row(review, month)})
    if review.status == MonthReview.STATUS_REVIEWED:
        return _locked_error(month)
    data = _body(request)
    if "checks" in data and isinstance(data["checks"], dict):
        review.checks = {key: bool(data["checks"].get(key)) for key in CHECK_KEYS}
    if "notes" in data:
        review.notes = str(data["notes"] or "")[:5000]
    if action == "close":
        if not all(review.checks.get(key) for key in CHECK_KEYS):
            return error("Tick every item on the checklist before marking the month as reviewed.")
        if month >= periods.month_start(timezone.localdate()):
            return error("A month can be marked reviewed once it has ended.")
        review.status, review.reviewed_at, review.reviewed_by = MonthReview.STATUS_REVIEWED, timezone.now(), request.user
    review.save()
    return respond({"review": review_row(review, month)})


# ——— access ———
@csrf_exempt
@require_http_methods(["GET"])
def access_list(request):
    users = get_user_model().objects.filter(is_active=True).select_related("finance_access").order_by("email", "username")
    return respond(
        {
            "users": [
                {
                    "id": u.id,
                    "name": u.get_full_name(),
                    "email": u.email or u.username,
                    "is_superuser": u.is_superuser,
                    "role": finance_role(u),
                }
                for u in users
            ]
        }
    )


@csrf_exempt
@require_http_methods(["PUT", "PATCH"])
def access_detail(request, pk):
    user = get_user_model().objects.filter(pk=pk, is_active=True).first()
    if not user:
        return error("User not found.", status=404)
    if user.is_superuser:
        return error("Superusers always have full Financials access.")
    if user.pk == request.user.pk:
        return error("You can't change your own access.")
    role = _body(request).get("role") or ""
    if role and role not in dict(ROLE_CHOICES):
        return error("Unknown role.")
    if role:
        FinanceAccess.objects.update_or_create(user=user, defaults={"role": role, "granted_by": request.user})
    else:
        FinanceAccess.objects.filter(user=user).delete()
    return respond({"id": user.pk, "role": role})


@csrf_exempt
def review_close(request, month):
    return review_detail(request, month, "close")


@csrf_exempt
def review_reopen(request, month):
    return review_detail(request, month, "reopen")


def entry(view):
    return protect(require_role(ENTRY)(view))


def full(view):
    return protect(require_role(FULL)(view))


def admin(view):
    return protect(require_role(ADMIN)(view))


urlpatterns = [
    path("meta/", protect(meta)),
    # expenses (entry users: their own only)
    path("expenses/", entry(expenses)),
    path("expenses/export/", entry(expenses_export)),
    path("expenses/<int:pk>/", entry(expense_detail)),
    path("expenses/<int:pk>/receipt/", entry(expense_receipt)),
    path("expenses/<int:pk>/verify/", full(expense_verify)),
    path("expenses/<int:pk>/skip/", full(expense_skip)),
    path("categories/", entry(categories)),
    path("categories/<int:pk>/", admin(category_detail)),
    path("import/", full(import_expenses)),
    path("import/batches/", full(import_batches)),
    path("import/batches/<int:pk>/", full(import_batch_undo)),
    # full access
    path("contractor-pay/", full(contractor_pay_view)),
    path("payroll/", full(payroll)),
    path("payroll/<int:pk>/", full(payroll_detail)),
    path("payroll/<int:pk>/receipt/", full(payroll_receipt)),
    path("recurring/", full(recurring_list)),
    path("recurring/<int:pk>/", full(recurring_detail)),
    path("monthly-totals/", full(monthly_totals)),
    path("monthly-totals/<int:pk>/", full(monthly_total_detail)),
    path("summary/", full(summary)),
    path("pnl/", full(pnl)),
    path("cash/", full(cash)),
    path("accounts/", full(accounts)),
    path("accounts/<int:pk>/", full(account_detail)),
    path("accounts/<int:pk>/balances/", full(account_balance)),
    path("balances/<int:pk>/", full(balance_detail)),
    path("report/", full(report)),
    path("reviews/", full(reviews)),
    path("reviews/<str:month>/", full(review_detail)),
    path("reviews/<str:month>/close/", full(review_close)),
    path("reviews/<str:month>/reopen/", full(review_reopen)),
    # admin
    path("access/", admin(access_list)),
    path("access/<int:pk>/", admin(access_detail)),
]
