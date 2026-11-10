"""Reporting periods: day / week / month / quarter / year / custom, with comparison periods.

Weeks run Sunday to Saturday, like the contractor pay weeks. A day or week is
compared with the one before and with the same weekday(s) 52 weeks earlier.

All dates are local (settings.TIME_ZONE). A period that is still in progress is
cut at today, and its comparison periods are cut at the same offset, so
"October 1–13" is compared with "September 1–13", not the whole of September.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from django.utils import timezone

KINDS = ("day", "week", "month", "quarter", "year", "custom")
SHORT_KINDS = ("day", "week")  # counted in days, not months
DAYS = {"day": 1, "week": 7}
SAME_WEEKDAY_LAST_YEAR = 364  # 52 weeks: Tuesday is compared with a Tuesday
PRESETS = {"last_3y": ("Last 3 years", 36), "last_5y": ("Last 5 years", 60), "lifetime": ("Lifetime", None)}


def today() -> date:
    return timezone.localdate()


def month_start(value: date) -> date:
    return value.replace(day=1)


def add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month = divmod(index, 12)
    day = min(value.day, calendar.monthrange(year, month + 1)[1])
    return date(year, month + 1, day)


def month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def months_between(start: date, end: date) -> list[date]:
    """First-of-month dates for every month touched by [start, end]."""
    out = []
    cursor = month_start(start)
    while cursor <= end:
        out.append(cursor)
        cursor = add_months(cursor, 1)
    return out


def aware(value: date) -> datetime:
    return timezone.make_aware(datetime.combine(value, time.min))


@dataclass(frozen=True)
class Period:
    kind: str
    start: date
    end: date  # inclusive
    full_end: date  # natural end of the period (end may be cut at today)
    preset: str = ""  # last_3y / last_5y / lifetime: a custom range with a name

    @property
    def start_dt(self) -> datetime:
        return aware(self.start)

    @property
    def end_dt(self) -> datetime:
        """Exclusive upper bound."""
        return aware(self.end + timedelta(days=1))

    @property
    def partial(self) -> bool:
        return self.end < self.full_end

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def label(self) -> str:
        if self.preset:
            return f"{PRESETS[self.preset][0]} ({_range_label(self.start, self.end)})"
        if self.kind == "day":
            return f"{self.start:%a}, {self.start:%b} {self.start.day}, {self.start.year}"
        if self.kind == "week":
            text = f"Week of {self.start:%b} {self.start.day}, {self.start.year}"
            return f"{text} ({_range_label(self.start, self.end)})" if self.partial else f"{text} ({_range_label(self.start, self.full_end)})"
        if self.kind == "month":
            text = self.start.strftime("%B %Y")
        elif self.kind == "quarter":
            text = f"Q{(self.start.month - 1) // 3 + 1} {self.start.year}"
        elif self.kind == "year":
            text = str(self.start.year)
        else:
            return _range_label(self.start, self.end)
        if self.partial:
            return f"{text} ({_range_label(self.start, self.end)})"
        return text

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "preset": self.preset,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "full_end": self.full_end.isoformat(),
            "partial": self.partial,
            "label": self.label,
        }

    def shifted(self, *, months: int = 0, days: int = 0) -> "Period":
        """Same-shaped period moved back/forward, cut at the same offset when partial."""
        if self.kind == "custom":
            start = self.start + timedelta(days=days)
            return Period("custom", start, start + timedelta(days=self.days - 1), start + timedelta(days=self.days - 1))
        if self.kind in SHORT_KINDS:
            start, full_end = self.start + timedelta(days=days), self.full_end + timedelta(days=days)
            return Period(self.kind, start, start + timedelta(days=self.days - 1), full_end)
        start = add_months(self.start, months)
        full_end = month_end(add_months(self.full_end.replace(day=1), months))
        end = start + timedelta(days=self.days - 1) if self.partial else full_end
        return Period(self.kind, start, min(end, full_end), full_end)

    def whole(self) -> "Period":
        """The full period even when it's cut at today (e.g. all of this month's planned visits)."""
        return replace(self, end=self.full_end)

    def previous(self) -> "Period":
        if self.kind == "custom":
            return self.shifted(days=-self.days)
        if self.kind in SHORT_KINDS:
            return self.shifted(days=-DAYS[self.kind])
        return self.shifted(months=-{"month": 1, "quarter": 3, "year": 12}[self.kind])

    def last_year(self) -> "Period":
        if self.kind == "custom":
            start = add_months(self.start, -12)
            end = add_months(self.end, -12)
            return Period("custom", start, end, end)
        if self.kind in SHORT_KINDS:
            return self.shifted(days=-SAME_WEEKDAY_LAST_YEAR)
        return self.shifted(months=-12)

    def step(self, direction: int) -> "Period":
        """Next/previous full period for the < Previous | Next > arrows."""
        if self.kind == "custom":
            return Period("custom", self.start + timedelta(days=self.days * direction), self.end + timedelta(days=self.days * direction), self.end + timedelta(days=self.days * direction))
        if self.kind in SHORT_KINDS:
            return build(self.kind, self.start + timedelta(days=DAYS[self.kind] * direction))
        months = {"month": 1, "quarter": 3, "year": 12}[self.kind] * direction
        return build(self.kind, add_months(self.start, months))


def _range_label(start: date, end: date) -> str:
    if start.year == end.year:
        if start.month == end.month:
            return f"{start.strftime('%b')} {start.day}–{end.day}, {start.year}"
        return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {start.year}"
    return f"{start.strftime('%b')} {start.day}, {start.year} – {end.strftime('%b')} {end.day}, {end.year}"


def week_start(value: date) -> date:
    """The Sunday on or before `value`."""
    return value - timedelta(days=(value.weekday() + 1) % 7)


def build(kind: str, anchor: date, *, end: date | None = None) -> Period:
    if kind == "day":
        start = full_end = anchor
    elif kind == "week":
        start = week_start(anchor)
        full_end = start + timedelta(days=6)
    elif kind == "quarter":
        start = date(anchor.year, (anchor.month - 1) // 3 * 3 + 1, 1)
        full_end = month_end(add_months(start, 2))
    elif kind == "year":
        start = date(anchor.year, 1, 1)
        full_end = date(anchor.year, 12, 31)
    elif kind == "custom":
        start = anchor
        full_end = end or anchor
        if full_end < start:
            start, full_end = full_end, start
        return Period("custom", start, full_end, full_end)
    else:
        start = month_start(anchor)
        full_end = month_end(start)
    current = today()
    cut = min(full_end, current) if start <= current else full_end
    return Period(kind, start, cut, full_end)


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def first_data_day() -> date:
    """The first invoice or visit, where Lifetime starts."""
    from django.db.models import Min

    from operations.models import Invoice, Visit

    firsts = [
        Invoice.objects.exclude(invoice_status="draft").aggregate(m=Min("issued_date"))["m"],
        Visit.objects.aggregate(m=Min("start_at"))["m"],
    ]
    firsts = [timezone.localtime(value).date() for value in firsts if value]
    return min(firsts) if firsts else today()


def preset(key: str) -> Period:
    months = PRESETS[key][1]
    start = add_months(today(), -months) + timedelta(days=1) if months else first_data_day()
    return replace(build("custom", start, end=today()), preset=key)


def from_request(request) -> Period:
    """?period=day|week|month|quarter|year|custom|last_3y|last_5y|lifetime&anchor=YYYY-MM-DD (or from/to for custom). Default: current month."""
    kind = request.GET.get("period") or "month"
    if kind in PRESETS:
        return preset(kind)
    if kind not in KINDS:
        kind = "month"
    if kind == "custom":
        start = _parse_date(request.GET.get("from")) or month_start(today())
        end = _parse_date(request.GET.get("to")) or today()
        return build("custom", start, end=end)
    return build(kind, _parse_date(request.GET.get("anchor")) or today())


def comparisons(period: Period) -> dict[str, Period]:
    if period.preset == "lifetime":
        return {}  # nothing comes before all of the data
    return {"previous": period.previous(), "last_year": period.last_year()}
