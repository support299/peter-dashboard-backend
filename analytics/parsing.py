from decimal import Decimal, InvalidOperation
from datetime import date, datetime

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime


def parse_dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed


def parse_d(value):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return parse_date(str(value)[:10])


def parse_decimal(value, default=None):
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def as_dict(value):
    return value if isinstance(value, dict) else {}
