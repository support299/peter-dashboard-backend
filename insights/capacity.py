"""
Cleaner-hours capacity from Jobber (Peter, Nov 2026). Groundwork: not shown to Cassidy until it has
been checked against a real week with Peter.

Booked hours  = the cleaner-hours written in each visit's Instructions ("4hr job", "5h job",
                "3.5 hours"). The number is the whole visit's labour, however many cleaners go.
                Each visit counts once; cancelled visits don't count. A range ("8-10h job") counts
                the upper number, so free time is never overstated; it is listed for review, like
                visits with no hours or several different numbers.
Available     = that day's Jobber task "Available Cleaners": each cleaner once × 6.5 h. The people
                assigned to the task are used when the office assigns them; otherwise the names
                are read from the title ("Available Cleaners: Vicky ATL, Leanna TL, …"). No task,
                or no names: "Availability unconfirmed".
Bookable      = available − booked, never below 0; anything over is a Shortfall.

Available hours are one company total (the task covers everyone). Absences come from the Hub's
leave records only, never from Jobber "(Absence)" tasks, so nothing is counted twice.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

HOURS_PER_CLEANER = Decimal("6.5")
CAPACITY_TITLE = "available cleaners"

_NUMBER = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"\s*(?:h\b|hr\b|hrs\b|hour\b|hours\b|heure\b|heures\b|h(?=\s*job))"
RANGE = re.compile(_NUMBER + r"\s*(?:-|–|to|à)\s*" + _NUMBER + _UNIT, re.I)
SINGLE = re.compile(_NUMBER + _UNIT, re.I)
SUFFIXES = re.compile(r"\b(?:A?TL|team lead(?:er)?)\b\.?", re.I)


def is_capacity_task(title: str | None) -> bool:
    return (title or "").strip().lower().startswith(CAPACITY_TITLE)


def _num(text: str) -> Decimal:
    return Decimal(text.replace(",", "."))


def parse_hours(text: str | None) -> tuple[Decimal | None, str]:
    """(hours, status): ok, range (upper number used), missing, or unclear (several different numbers)."""
    text = text or ""
    ranges = RANGE.findall(text)
    if ranges:
        values = {max(_num(a), _num(b)) for a, b in ranges}
        return (values.pop(), "range") if len(values) == 1 else (None, "unclear")
    values = {_num(n) for n in SINGLE.findall(text)}
    values.discard(Decimal(0))
    if not values:
        return None, "missing"
    if len(values) > 1:
        return None, "unclear"
    return values.pop(), "ok"


def task_cleaners(task) -> tuple[list[str], str]:
    """(people, how): Jobber users assigned to the task when there are any, else names from the title."""
    payload = task.source_payload or {}
    assigned = [n.get("id") for n in ((payload.get("assignedUsers") or {}).get("nodes") or []) if n and n.get("id")]
    if assigned:
        return sorted(set(assigned)), "assigned"
    title = task.title or ""
    names = title.split(":", 1)[1] if ":" in title else ""
    people = []
    for raw in re.split(r"[,;/&\n]| and ", names):
        name = SUFFIXES.sub("", raw).strip(" .-–()")
        if name and name.lower() not in {n.lower() for n in people}:
            people.append(name)
    return people, "title" if people else "none"


def capacity_days(integration, start: date, end: date, division: str = "") -> list[dict]:
    """One row per day: booked hours (and by division), available hours, bookable, shortfall, and what to review."""
    from django.utils import timezone

    from operations.models import JobberTask, Visit

    days = {start + timedelta(days=i): None for i in range((end - start).days + 1)}
    lo = timezone.make_aware(timezone.datetime.combine(start, timezone.datetime.min.time()))
    hi = timezone.make_aware(timezone.datetime.combine(end + timedelta(days=1), timezone.datetime.min.time()))
    booked = defaultdict(lambda: Decimal(0))
    by_division = defaultdict(lambda: defaultdict(Decimal))
    review = defaultdict(list)
    visits = Visit.objects.filter(integration=integration, start_at__gte=lo, start_at__lt=hi, is_cancelled=False).exclude(
        client__is_internal=True
    )
    if division:
        visits = visits.filter(division=division)
    for visit in visits.select_related("client").only("id", "start_at", "instructions", "title", "division", "client__name"):
        day = timezone.localtime(visit.start_at).date()
        hours, status = parse_hours(visit.instructions)
        if hours is not None:
            booked[day] += hours
            by_division[day][visit.division or "unassigned"] += hours
        if status != "ok":
            review[day].append(
                {"visit_id": visit.id, "client": visit.client.name if visit.client else "", "title": visit.title, "status": status, "hours": hours}
            )

    tasks = {}
    for task in JobberTask.objects.filter(integration=integration, due_at__gte=lo, due_at__lt=hi).order_by("due_at"):
        if is_capacity_task(task.title):
            tasks.setdefault(timezone.localtime(task.due_at).date(), task)

    out = []
    for day in days:
        task = tasks.get(day)
        people, how = task_cleaners(task) if task else ([], "none")
        available = HOURS_PER_CLEANER * len(people) if people else None
        row = {
            "date": day,
            "booked": booked[day],
            "booked_by_division": dict(by_division[day]),
            "cleaners": people,
            "cleaners_from": how,
            "available": available,
            "availability": "confirmed" if available is not None else "unconfirmed",
            "bookable": max(available - booked[day], Decimal(0)) if available is not None else None,
            "shortfall": max(booked[day] - available, Decimal(0)) if available is not None else None,
            "task_notes": (task.instructions or "").strip() if task else "",
            "review": review[day],
        }
        if division:
            # Available hours are company-wide: with a division picked, only booked hours are filtered.
            row["bookable"] = row["shortfall"] = None
        out.append(row)
    return out


def week_totals(days: list[dict]) -> dict:
    """Bookable and shortfall add up separately: Tuesday's openings don't cover Wednesday's shortage."""
    confirmed = [d for d in days if d["available"] is not None]
    # With a division picked, bookable/shortfall are blank per day (available is company-wide): keep them blank here too.
    open_days = [d for d in confirmed if d["bookable"] is not None]
    return {
        "booked": sum((d["booked"] for d in days), Decimal(0)),
        "available": sum((d["available"] for d in confirmed), Decimal(0)) if confirmed else None,
        "bookable": sum((d["bookable"] for d in open_days), Decimal(0)) if open_days else None,
        "shortfall": sum((d["shortfall"] for d in open_days), Decimal(0)) if open_days else None,
        "unconfirmed_days": len(days) - len(confirmed),
        "to_review": sum(len(d["review"]) for d in days),
    }
