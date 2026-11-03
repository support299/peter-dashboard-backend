"""Team leaders: who led each visit, and complaints, hours and touch-ups per leader.

A team leader is anyone with the position "Team Leader" in the Hub, former staff
included so older visits stay with the person who led them. A visit's leader is
the one team leader assigned to it in Jobber. With none, or more than one, it has
no leader: we don't guess.

Hub hours are per person per day with no link to a visit, so a person's day is
split across their completed visits that day by scheduled length. Hours on a day
with no visit, or on a visit with no leader, go to "No team leader", which keeps
the table's total equal to the Labour hours card.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Q
from django.utils import timezone

from analytics.hub_models import HubEmployee
from insights.metrics import TOUCH_UP_HOURLY_COST, touch_up_labour_hours
from operations.models import Employee, Visit, VisitAssignment

POSITION = "Team Leader"
NO_LEADER = ""
NO_LEADER_LABEL = "No team leader"


def leader_employees(integration) -> dict:
    """{Employee id: Employee} for everyone holding the position."""
    jobber_ids = HubEmployee.objects.filter(position__iexact=POSITION).exclude(jobber_id="").values_list("jobber_id", flat=True)
    return {e.id: e for e in Employee.objects.filter(integration=integration, external_id__in=list(jobber_ids))}


def assign_team_leaders(integration) -> dict:
    leaders = leader_employees(integration)
    on_visit = defaultdict(set)
    rows = VisitAssignment.objects.filter(visit__integration=integration, employee_id__in=leaders).values_list("visit_id", "employee_id")
    for visit_id, employee_id in rows:
        on_visit[visit_id].add(employee_id)
    by_leader = defaultdict(list)
    for visit_id, ids in on_visit.items():
        if len(ids) == 1:
            by_leader[next(iter(ids))].append(visit_id)
    led = {visit_id for ids in by_leader.values() for visit_id in ids}
    visits = Visit.objects.filter(integration=integration)
    cleared = visits.filter(team_leader__isnull=False).exclude(pk__in=led).update(team_leader=None)
    changed = 0
    for leader_id, ids in by_leader.items():
        changed += visits.filter(pk__in=ids).exclude(team_leader_id=leader_id).update(team_leader_id=leader_id)
    return {"leaders": len(leaders), "visits_with_leader": len(led), "more_than_one_leader": len(on_visit) - len(led), "changed": changed, "cleared": cleared}


def _day_plans(metrics, period) -> dict:
    """{(Jobber user id, day): [(leader's Jobber id, minutes)]} from completed visits."""
    plans = defaultdict(list)
    rows = (
        VisitAssignment.objects.filter(
            visit__integration=metrics.integration, visit__is_complete=True, visit__start_at__gte=period.start_dt, visit__start_at__lt=period.end_dt
        )
        .exclude(visit__client__is_internal=True)
        .values_list("employee__external_id", "visit__start_at", "visit__duration_minutes", "visit__team_leader__external_id")
    )
    for employee, start, minutes, leader in rows:
        plans[(employee, timezone.localtime(start).date())].append((leader or NO_LEADER, minutes or 0))
    return plans


def split_hours(metrics, period) -> dict:
    """{leader's Jobber id or NO_LEADER: hours}."""
    plans = _day_plans(metrics, period)
    out = defaultdict(Decimal)
    for employee, day, hours in metrics.hours(period).values_list("jobber_user_id", "date", "hours_worked"):
        hours = Decimal(hours or 0)
        plan = plans.get((employee, day))
        if not plan:
            out[NO_LEADER] += hours
            continue
        total = sum(minutes for _, minutes in plan)
        for leader, minutes in plan:
            share = Decimal(minutes) / total if total else Decimal(1) / len(plan)
            out[leader] += hours * share
    return out


def team_leader_table(metrics, period) -> list:
    """One row per leader plus "No team leader". Hours are left out when a division is selected (they have no division)."""
    rows = defaultdict(lambda: {"visits": 0, "touch_ups": 0, "touch_up_hours": Decimal(0), "complaints": 0, "hours": None})
    names = {}

    visits = metrics._visits(period).filter(Q(is_complete=True) | Q(is_touch_up=True, is_cancelled=False))
    counted = visits.values("team_leader__external_id", "team_leader__full_name").annotate(
        done=Count("id", filter=Q(is_complete=True)), redos=Count("id", filter=Q(is_touch_up=True, is_cancelled=False))
    )
    for row in counted:
        key = row["team_leader__external_id"] or NO_LEADER
        names[key] = row["team_leader__full_name"]
        rows[key]["visits"] += row["done"]
        rows[key]["touch_ups"] += row["redos"]
    touch = metrics._visits(period).filter(is_touch_up=True, is_complete=True)
    for leader in touch.values_list("team_leader__external_id", flat=True).distinct():
        rows[leader or NO_LEADER]["touch_up_hours"] = touch_up_labour_hours(touch.filter(team_leader__external_id=leader) if leader else touch.filter(team_leader__isnull=True))

    for leader in metrics.complaints(period).values_list("team_leader", flat=True):
        leader = leader or {}
        key = leader.get("jobber_user_id") or (f"name:{leader['name']}" if leader.get("name") else NO_LEADER)
        names.setdefault(key, leader.get("name"))
        rows[key]["complaints"] += 1

    if not metrics.division and metrics._has("hours", period):
        for key, hours in split_hours(metrics, period).items():
            rows[key]["hours"] = hours.quantize(Decimal("0.01"))
        for row in rows.values():
            row["hours"] = row["hours"] or Decimal(0)
        cents = metrics.value("labour_hours", period) - sum(row["hours"] for row in rows.values())
        if cents and rows:  # rounding: keep the table equal to the Labour hours card
            max(rows.values(), key=lambda row: row["hours"])["hours"] += cents

    out = []
    for key, row in rows.items():
        hours = row.pop("touch_up_hours")
        out.append(
            {
                "key": key,
                "team_leader": NO_LEADER_LABEL if key == NO_LEADER else names.get(key) or "—",
                **row,
                "touch_up_hours": hours,
                "touch_up_cost": (hours * TOUCH_UP_HOURLY_COST).quantize(Decimal("0.01")),
            }
        )
    return sorted(out, key=lambda r: (r["key"] == NO_LEADER, r["team_leader"]))
