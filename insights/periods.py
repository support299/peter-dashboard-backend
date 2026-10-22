"""Reporting periods: month / quarter / year / custom, with comparison periods.

All dates are local (settings.TIME_ZONE). A period that is still in progress is
cut at today, and its comparison periods are cut at the same offset, so
"October 1–13" is compared with "September 1–13", not the whole of September.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

KINDS = ("month", "quarter", "year", "custom")


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
        start = add_months(self.start, months)
        full_end = month_end(add_months(self.full_end.replace(day=1), months))
        end = start + timedelta(days=self.days - 1) if self.partial else full_end
        return Period(self.kind, start, min(end, full_end), full_end)

    def previous(self) -> "Period":
        if self.kind == "custom":
            return self.shifted(days=-self.days)
        return self.shifted(months=-{"month": 1, "quarter": 3, "year": 12}[self.kind])

    def last_year(self) -> "Period":
        if self.kind == "custom":
            start = add_months(self.start, -12)
            end = add_months(self.end, -12)
            return Period("custom", start, end, end)
        return self.shifted(months=-12)

    def step(self, direction: int) -> "Period":
        """Next/previous full period for the < Previous | Next > arrows."""
        if self.kind == "custom":
            return Period("custom", self.start + timedelta(days=self.days * direction), self.end + timedelta(days=self.days * direction), self.end + timedelta(days=self.days * direction))
        months = {"month": 1, "quarter": 3, "year": 12}[self.kind] * direction
        return build(self.kind, add_months(self.start, months))


def _range_label(start: date, end: date) -> str:
    if start.year == end.year:
        if start.month == end.month:
            return f"{start.strftime('%b')} {start.day}–{end.day}, {start.year}"
        return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {start.year}"
    return f"{start.strftime('%b')} {start.day}, {start.year} – {end.strftime('%b')} {end.day}, {end.year}"


def build(kind: str, anchor: date, *, end: date | None = None) -> Period:
    if kind == "quarter":
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


def from_request(request) -> Period:
    """?period=month|quarter|year|custom&anchor=YYYY-MM-DD (or from/to for custom). Default: current month."""
    kind = request.GET.get("period") or "month"
    if kind not in KINDS:
        kind = "month"
    if kind == "custom":
        start = _parse_date(request.GET.get("from")) or month_start(today())
        end = _parse_date(request.GET.get("to")) or today()
        return build("custom", start, end=end)
    return build(kind, _parse_date(request.GET.get("anchor")) or today())


def comparisons(period: Period) -> dict[str, Period]:
    return {"previous": period.previous(), "last_year": period.last_year()}
