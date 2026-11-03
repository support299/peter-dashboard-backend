"""CSV / Excel expense import.

Columns are matched by name (English or French), in any order. Only a date and
an amount (or total) are required. Rows that match an existing expense (same
date, total and vendor), or another row in the same file, are skipped as duplicates.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from financials.ledger import is_locked
from financials.models import (
    DIVISION_SHARED,
    GROUP_CHOICES,
    EXPENSE_DIVISION_CHOICES,
    PAYMENT_METHOD_CHOICES,
    SOURCE_IMPORT,
    STATUS_PAID,
    STATUS_UNPAID,
    Category,
    Expense,
    ImportBatch,
)

MAX_ROWS = 5000

COLUMNS = {
    "date": ["date", "expense date", "transaction date", "date de transaction"],
    "vendor": ["vendor", "supplier", "vendor / supplier", "vendor/supplier", "payee", "merchant", "fournisseur"],
    "description": ["description", "details", "memo", "détails"],
    "amount": ["amount", "amount before tax", "before tax", "subtotal", "sub-total", "montant", "montant avant taxes"],
    "gst": ["gst", "tps", "gst/hst"],
    "qst": ["qst", "tvq"],
    "total": ["total", "total amount", "amount with tax", "montant total"],
    "category": ["category", "catégorie", "categorie"],
    "group": ["group", "groupe", "category group"],
    "subcategory": ["subcategory", "sub-category", "sub category", "sous-catégorie", "sous-categorie"],
    "division": ["division"],
    "payment_method": ["payment method", "payment", "method", "mode de paiement"],
    "status": ["status", "paid", "paid / unpaid", "statut"],
    "notes": ["notes", "note", "comments"],
}
TEMPLATE_HEADERS = [
    "Date",
    "Vendor",
    "Description",
    "Amount before tax",
    "GST",
    "QST",
    "Total",
    "Category",
    "Subcategory",
    "Division",
    "Payment method",
    "Status",
    "Notes",
]


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def read_table(upload) -> list[list]:
    name = (upload.name or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        sheet = load_workbook(upload, read_only=True, data_only=True).worksheets[0]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")  # Excel "CSV" saved on Windows
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def parse_money(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = str(value).replace("\xa0", "").replace(" ", "").replace("$", "").replace("CAD", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if "," in text and "." not in text:
        text = text.replace(",", ".")  # French decimal comma: 1234,56
    text = text.replace(",", "")
    try:
        amount = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"“{value}” is not an amount")
    return -amount if negative else amount


def parse_date(value, order: str = "dmy") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):  # Excel serial number
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing date")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", text)
    if match:
        a, b, year = (int(x) for x in match.groups())
        year += 2000 if year < 100 else 0
        day, month = (a, b) if order == "dmy" else (b, a)
        try:
            return date(year, month, day)
        except ValueError:
            pass
    raise ValueError(f"“{text}” is not a date")


class Lookup:
    """Match category, division and payment method text to their keys."""

    def __init__(self):
        cats = list(Category.objects.filter(is_active=True))
        self.categories = {}
        self.subcategories = {}
        for c in cats:
            target = self.subcategories if c.parent_id else self.categories
            target.setdefault(_norm(c.name), []).append(c)
        self.groups = {}
        for key, label in GROUP_CHOICES:
            for name in (key, label, label.split(" (")[0]):
                self.groups[_norm(name)] = key
        self.groups.update({"vehicle": "vehicles", "cost of sales": "cogs", "operating": "opex", "other": "other"})
        self.divisions = {}
        for key, label in EXPENSE_DIVISION_CHOICES:
            self.divisions[_norm(key)] = key
            self.divisions[_norm(label)] = key
        self.divisions[_norm("Specialized")] = "specialized"
        self.divisions[_norm("Rentals")] = "rentals"
        self.methods = {}
        for key, label in PAYMENT_METHOD_CHOICES:
            self.methods[_norm(key)] = key
            self.methods[_norm(label)] = key

    def _group(self, text):
        if not text:
            return None
        group = self.groups.get(_norm(text))
        if not group:
            raise ValueError(f"unknown group “{text}”")
        return group

    def category(self, cat_text, sub_text, default: Category | None, group_text=""):
        group = self._group(group_text)
        # "Vehicles: Insurance" or "Vehicles > Insurance" picks the group in the Category column.
        for sep in (":", "›", ">"):
            if cat_text and sep in cat_text:
                head, tail = (part.strip() for part in cat_text.split(sep, 1))
                if _norm(head) in self.groups:
                    group, cat_text = self.groups[_norm(head)], tail
                break
        category = None
        if cat_text:
            matches = [c for c in self.categories.get(_norm(cat_text), []) if not group or c.group == group]
            if len(matches) > 1:
                groups = ", ".join(sorted({c.get_group_display() for c in matches}))
                raise ValueError(f"“{cat_text}” exists in more than one group ({groups}): add a Group column or write “Group: {cat_text}”")
            if matches:
                category = matches[0]
            else:
                # A subcategory name in the Category column.
                subs = [c for c in self.subcategories.get(_norm(cat_text), []) if not group or c.group == group]
                if len(subs) == 1:
                    return subs[0].parent, subs[0]
                if not default:
                    raise ValueError(f"unknown category “{cat_text}”")
        if category is None:
            category = default
        if category is None:
            raise ValueError("no category")
        sub = None
        if sub_text:
            sub = next((c for c in self.subcategories.get(_norm(sub_text), []) if c.parent_id == category.id), None)
            if sub is None:
                raise ValueError(f"“{sub_text}” is not a subcategory of {category.name}")
        return category, sub


def _columns(header: list) -> dict:
    found = {}
    for index, cell in enumerate(header):
        name = _norm(cell)
        for key, names in COLUMNS.items():
            if name in names and key not in found:
                found[key] = index
    return found


def parse(upload, *, order="dmy", default_category: Category | None = None) -> dict:
    table = [row for row in read_table(upload) if any(cell not in (None, "") for cell in row)]
    if not table:
        raise ValueError("The file is empty.")
    columns = _columns(table[0])
    if "date" not in columns or not ({"amount", "total"} & columns.keys()):
        raise ValueError("The first row must have column names, including Date and Amount (or Total). Download the template to see the format.")
    if len(table) - 1 > MAX_ROWS:
        raise ValueError(f"Import up to {MAX_ROWS} rows at a time.")

    lookup = Lookup()
    seen = set()
    rows = []
    for number, raw in enumerate(table[1:], start=2):
        cell = lambda key: (raw[columns[key]] if key in columns and columns[key] < len(raw) else None)  # noqa: E731
        text = lambda key: str(cell(key) or "").strip()  # noqa: E731
        row = {"row": number, "errors": [], "duplicate": False}
        try:
            row["date"] = parse_date(cell("date"), order)
        except ValueError as exc:
            row["errors"].append(str(exc))
        try:
            gst, qst = parse_money(cell("gst")) or Decimal(0), parse_money(cell("qst")) or Decimal(0)
            amount, total = parse_money(cell("amount")), parse_money(cell("total"))
            if amount is None and total is None:
                raise ValueError("missing amount")
            if amount is None:
                amount = total - gst - qst
            row.update(amount=amount, gst=gst, qst=qst, total=amount + gst + qst)
            if total is not None and abs(total - row["total"]) > Decimal("0.05"):
                row["errors"].append(f"amount + taxes ({row['total']}) doesn't equal the total ({total})")
        except ValueError as exc:
            row["errors"].append(str(exc))
        try:
            category, sub = lookup.category(text("category"), text("subcategory"), default_category, text("group"))
            row.update(category=category, subcategory=sub)
        except ValueError as exc:
            row["errors"].append(str(exc))
        division_text = _norm(text("division"))
        row["division"] = lookup.divisions.get(division_text, DIVISION_SHARED) if division_text else DIVISION_SHARED
        if division_text and division_text not in lookup.divisions:
            row["errors"].append(f"unknown division “{text('division')}”")
        method_text = _norm(text("payment_method"))
        row["payment_method"] = lookup.methods.get(method_text, "other") if method_text else ""
        status_text = _norm(text("status"))
        row["status"] = STATUS_UNPAID if status_text in ("unpaid", "no", "non", "non payé", "non paye", "false", "0") else STATUS_PAID
        row.update(vendor=text("vendor")[:200], description=text("description")[:500], notes=text("notes"))
        if row.get("date") and is_locked(row["date"]):
            row["errors"].append(f"{row['date']:%B %Y} is marked reviewed (reopen it to add expenses)")

        if not row["errors"]:
            key = (row["date"], row["total"], row["vendor"].lower())
            if key in seen or Expense.objects.filter(date=row["date"], total=row["total"], vendor__iexact=row["vendor"]).exists():
                row["duplicate"] = True
            seen.add(key)
        rows.append(row)
    # Only a total and no tax columns: the tax is counted as part of the cost. Say so before importing.
    taxes_missing = "amount" not in columns and not ({"gst", "qst"} & columns.keys())
    return {"columns": sorted(columns), "rows": rows, "taxes_missing": taxes_missing}


def preview_row(row: dict) -> dict:
    return {
        "row": row["row"],
        "date": row.get("date"),
        "vendor": row.get("vendor", ""),
        "description": row.get("description", ""),
        "amount": row.get("amount"),
        "gst": row.get("gst"),
        "qst": row.get("qst"),
        "total": row.get("total"),
        "category": row["category"].name if row.get("category") else "",
        "subcategory": row["subcategory"].name if row.get("subcategory") else "",
        "division": row.get("division"),
        "status": row.get("status"),
        "errors": row["errors"],
        "duplicate": row["duplicate"],
    }


def commit(parsed: dict, *, file_name: str, user, include_duplicates=False) -> ImportBatch:
    batch = ImportBatch.objects.create(file_name=file_name[:255], created_by=user)
    imported = skipped = 0
    for row in parsed["rows"]:
        if row["errors"] or (row["duplicate"] and not include_duplicates):
            skipped += 1
            continue
        Expense.objects.create(
            date=row["date"],
            vendor=row["vendor"],
            description=row["description"],
            amount=row["amount"],
            gst=row["gst"],
            qst=row["qst"],
            category=row["category"],
            subcategory=row["subcategory"],
            division=row["division"],
            payment_method=row["payment_method"],
            status=row["status"],
            notes=row["notes"],
            source=SOURCE_IMPORT,
            import_batch=batch,
            created_by=user,
            updated_by=user,
        )
        imported += 1
    batch.rows_imported, batch.rows_skipped = imported, skipped
    batch.save(update_fields=["rows_imported", "rows_skipped"])
    return batch
