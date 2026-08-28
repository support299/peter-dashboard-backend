from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

TITLE_FIRST_CLEAN = (
    "first clean",
    "first-time",
    "first time",
    "initial clean",
    "new client clean",
    "move in",
    "move-in",
)
TITLE_DEEP_CLEAN = ("deep clean", "deep-clean", "deepclean")
CANCELLED_STATUSES = {"CANCELLED", "CANCELED"}
COMPLETED_STATUSES = {"COMPLETED", "COMPLETE", "DONE"}
RECURRING_TYPES = {"RECURRING", "RECURRING_JOB", "REPEAT", "REPEATING"}
ONE_OFF_TYPES = {"ONE_OFF", "ONEOFF", "ONE-OFF", "ONCE"}


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = parse_datetime(str(value))
        if not parsed:
            date_only = parse_date(str(value)[:10])
            if date_only:
                parsed = datetime.combine(date_only, datetime.min.time())
    if not parsed:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.utc)
    return parsed


def clip(value, limit: int = 255) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def clean_name(value, limit: int = 255) -> str:
    text = clip(value, limit).strip()
    if not text or text in {"-", "—"}:
        return ""
    return text


def person_display_name(node: dict | None) -> str:
    node = node or {}
    first = clean_name(node.get("firstName"))
    last = clean_name(node.get("lastName"))
    full = " ".join(part for part in [first, last] if part)
    return full or clean_name(node.get("companyName")) or clean_name(node.get("name"))


def parse_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def format_street(address: dict | None) -> str:
    address = address or {}
    parts = [address.get("street") or address.get("street1") or "", address.get("street2") or ""]
    return clip(" ".join(part for part in parts if part), 1024)


def money_total(node: dict | None):
    """Prefer current Jobber money shape: amounts.total, then total. Never lead with deprecated cost."""
    if not node:
        return None
    amounts = node.get("amounts") if isinstance(node.get("amounts"), dict) else {}
    if amounts.get("total") is not None:
        return parse_decimal(amounts["total"])
    if node.get("total") is not None:
        return parse_decimal(node["total"])
    return parse_decimal(node.get("cost"))


def classify_title(title: str | None) -> tuple[bool, bool]:
    text = (title or "").lower()
    is_first = any(token in text for token in TITLE_FIRST_CLEAN)
    is_deep = any(token in text for token in TITLE_DEEP_CLEAN)
    return is_first, is_deep


def is_recurring_type(job_type: str | None) -> bool:
    return (job_type or "").upper() in RECURRING_TYPES


def is_one_off_type(job_type: str | None) -> bool:
    value = (job_type or "").upper()
    if value in ONE_OFF_TYPES:
        return True
    return bool(value) and value not in RECURRING_TYPES


def is_cancelled_status(status: str | None) -> bool:
    return (status or "").upper() in CANCELLED_STATUSES


def is_completed_status(status: str | None, is_complete: bool | None = None) -> bool:
    if is_complete:
        return True
    return (status or "").upper() in COMPLETED_STATUSES


def node_id(node):
    if not node or not isinstance(node, dict):
        return ""
    return node.get("id") or ""


def line_item_total(item: dict):
    item = item or {}
    for key in ("totalPrice", "total", "totalCost"):
        if item.get(key) is not None:
            return parse_decimal(item.get(key))
    return None


def line_item_unit_price(item: dict):
    item = item or {}
    for key in ("unitPrice", "unitCost"):
        if item.get(key) is not None:
            return parse_decimal(item.get(key))
    return None
