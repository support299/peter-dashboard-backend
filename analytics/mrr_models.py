"""MRR booked / cancelled jobs — Insights-owned records (not Airtable sync)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import models
from django.utils import timezone


TWOPLACES = Decimal("0.01")


def _d(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def normalize_frequency(raw: str) -> str:
    text = (raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    if text in {"monthly", "month"}:
        return "Monthly"
    if text in {"bi-weekly", "biweekly", "bi-week", "fortnightly"}:
        return "Bi-Weekly"
    if text in {"weekly", "week"}:
        return "Weekly"
    # Preserve original casing if unknown
    return (raw or "").strip() or "Monthly"


def compute_monthly_value(visit_value, frequency: str) -> Decimal:
    """
    Client formulas:
      Monthly   → visit value
      Bi-Weekly → visit value × 26 ÷ 12
      Weekly    → visit value × 52 ÷ 12
    """
    vv = _d(visit_value)
    freq = normalize_frequency(frequency)
    if freq == "Monthly":
        return vv.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    if freq == "Bi-Weekly":
        return (vv * Decimal(26) / Decimal(12)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    if freq == "Weekly":
        return (vv * Decimal(52) / Decimal(12)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    return vv.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def compute_months_lifespan(start: date | None, end: date | None) -> Decimal:
    if not start:
        return Decimal("0")
    finish = end or timezone.localdate()
    days = max((finish - start).days, 0)
    return (Decimal(days) / Decimal("30.4375")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_clv(monthly_value, start: date | None, end: date | None, status: str) -> Decimal:
    """CLV is calculated when the client cancels / goes inactive."""
    status_l = (status or "").strip().lower()
    if status_l in {"active", ""}:
        return Decimal("0.00")
    months = compute_months_lifespan(start, end)
    return (_d(monthly_value) * months).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def visits_per_month_for_frequency(frequency: str) -> Decimal:
    freq = normalize_frequency(frequency)
    if freq == "Weekly":
        return Decimal("4")  # display approx; formulas use 52/12
    if freq == "Bi-Weekly":
        return Decimal("2")
    return Decimal("1")


class MrrBooking(models.Model):
    STATUS_ACTIVE = "Active"
    STATUS_INACTIVE = "Inactive"
    STATUS_CANCELLED = "Cancelled"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_INACTIVE, "Inactive"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    SOURCE_MANUAL = "manual"
    SOURCE_LOCKIN = "lockin"
    SOURCE_IMPORT = "import"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_LOCKIN, "Lock-in"),
        (SOURCE_IMPORT, "Import"),
    ]

    name = models.CharField(max_length=255, db_index=True)
    start_date = models.DateField(null=True, blank=True, db_index=True)
    end_date = models.DateField(null=True, blank=True, db_index=True)
    contract = models.CharField(max_length=64, blank=True, default="Residential")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)
    frequency = models.CharField(max_length=64, blank=True, default="Monthly", db_index=True)
    plan_type = models.CharField(max_length=64, blank=True, default="")
    visits_per_month = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    visits_per_year = models.DecimalField(max_digits=10, decimal_places=2, default=12)
    hours_per_visit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    visit_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    monthly_value = models.DecimalField(max_digits=14, decimal_places=2, default=0, db_index=True)
    yearly_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    months_lifespan = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    years_lifespan = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    clv = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    method = models.CharField(max_length=120, blank=True, default="")
    cancellation_reason = models.CharField(max_length=255, blank=True, default="")
    client_comments = models.TextField(blank=True, default="")
    sales_person = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL, db_index=True)
    lockin = models.ForeignKey(
        "analytics.HubPendingLockIn",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mrr_bookings",
    )
    lockin_external_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # When True, lock-in re-import must not overwrite user edits.
    manual_override = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mrr_bookings_created",
    )

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [
            models.Index(fields=["status", "start_date"]),
            models.Index(fields=["status", "end_date"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"

    def recompute(self, *, save=False):
        self.frequency = normalize_frequency(self.frequency)
        self.monthly_value = compute_monthly_value(self.visit_value, self.frequency)
        self.yearly_value = (self.monthly_value * Decimal(12)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)

        freq = normalize_frequency(self.frequency)
        if freq == "Weekly":
            self.visits_per_year = Decimal("52")
            self.visits_per_month = (Decimal("52") / Decimal("12")).quantize(Decimal("0.01"))
        elif freq == "Bi-Weekly":
            self.visits_per_year = Decimal("26")
            self.visits_per_month = (Decimal("26") / Decimal("12")).quantize(Decimal("0.01"))
        else:
            self.visits_per_year = Decimal("12")
            self.visits_per_month = Decimal("1")

        end = self.end_date
        if self.status != self.STATUS_ACTIVE and not end:
            end = timezone.localdate()
        self.months_lifespan = compute_months_lifespan(self.start_date, end)
        self.years_lifespan = (self.months_lifespan / Decimal(12)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.clv = compute_clv(self.monthly_value, self.start_date, end, self.status)
        if save:
            self.save()
        return self
