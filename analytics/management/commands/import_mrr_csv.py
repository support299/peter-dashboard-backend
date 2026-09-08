"""Seed MrrBooking rows from the Airtable CSV export (one-time / refresh)."""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_date

from analytics.mrr_models import MrrBooking, normalize_frequency


def _money(raw) -> Decimal:
    text = str(raw or "").strip()
    if not text:
        return Decimal("0")
    text = text.replace("$", "").replace(",", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _date(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    parsed = parse_date(text)
    if parsed:
        return parsed
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _status(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text in {"cancelled", "canceled"}:
        return MrrBooking.STATUS_CANCELLED
    if text in {"inactive", "unactive", "paused"}:
        return MrrBooking.STATUS_INACTIVE
    return MrrBooking.STATUS_ACTIVE


def _hours(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


class Command(BaseCommand):
    help = "Import MRR bookings from the Airtable CSV export into Insights."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default="",
            help="Path to CSV (default: docs/MRR Booked _ Cancelled Jobs-All.csv)",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete existing import-sourced rows before loading",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report without writing",
        )

    def handle(self, *args, **options):
        path_arg = options["path"]
        if path_arg:
            csv_path = Path(path_arg)
        else:
            # Backend/analytics/... → repo root docs/
            csv_path = Path(__file__).resolve().parents[4] / "docs" / "MRR Booked _ Cancelled Jobs-All.csv"
            if not csv_path.exists():
                csv_path = Path(__file__).resolve().parents[3].parent / "docs" / "MRR Booked _ Cancelled Jobs-All.csv"

        if not csv_path.exists():
            raise CommandError(f"CSV not found: {csv_path}")

        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"Dry run: {len(rows)} rows from {csv_path}"))
            return

        created = 0
        with transaction.atomic():
            if options["replace"]:
                deleted, _ = MrrBooking.objects.filter(source=MrrBooking.SOURCE_IMPORT).delete()
                self.stdout.write(f"Deleted {deleted} previous import rows")

            for row in rows:
                name = (row.get("Name") or "").strip()
                if not name:
                    continue
                booking = MrrBooking(
                    name=name,
                    start_date=_date(row.get("Start Date")),
                    end_date=_date(row.get("End Date")),
                    contract=(row.get("Contract") or "Residential").strip() or "Residential",
                    status=_status(row.get("Status") or ""),
                    frequency=normalize_frequency(row.get("Frequency") or "Monthly"),
                    plan_type=(row.get("Plan Type") or "").strip(),
                    hours_per_visit=_hours(row.get("Hrs/V")),
                    visit_value=_money(row.get("Visit Value")),
                    method=(row.get("Method") or "").strip(),
                    cancellation_reason=(row.get("Cancellation Reason") or "").strip(),
                    client_comments=(row.get("Client's Comments") or row.get("Client’s Comments") or "").strip(),
                    sales_person=(row.get("Sales Person") or "").strip(),
                    notes=(row.get("Notes") or "").strip(),
                    source=MrrBooking.SOURCE_IMPORT,
                    manual_override=True,
                )
                # Keep Airtable notes extras lightly
                extras = []
                for key in (
                    "What were you not satisfied with?",
                    "Follow-up Needed",
                    "Follow-up Outcome",
                    "If Financial Reasons",
                    "If Scheduling Conflict",
                    "If Found Cheaper Service",
                    "If Temporary Pause",
                ):
                    val = (row.get(key) or "").strip()
                    if val:
                        extras.append(f"{key}: {val}")
                if extras:
                    booking.notes = (booking.notes + "\n" if booking.notes else "") + "\n".join(extras)
                booking.recompute()
                booking.save()
                created += 1

        self.stdout.write(self.style.SUCCESS(f"Imported {created} MRR bookings from {csv_path.name}"))
