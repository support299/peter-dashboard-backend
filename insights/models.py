from decimal import Decimal

from django.conf import settings
from django.db import models

from integrations.models import Integration
from operations.models import Client, Job

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_SCHEDULED_RESUME = "scheduled_resume"
STATUS_CANCELLED = "cancelled"
PLAN_STATUS_CHOICES = [
    (STATUS_ACTIVE, "Active"),
    (STATUS_PAUSED, "Paused"),
    (STATUS_SCHEDULED_RESUME, "Scheduled to resume"),
    (STATUS_CANCELLED, "Cancelled"),
]
# Paused and scheduled-to-resume plans still count as recurring clients, but not in Active MRR.
PAUSED_STATUSES = {STATUS_PAUSED, STATUS_SCHEDULED_RESUME}


class InsightsSetting(models.Model):
    """Small key/value store (MRR tracking start date, future alert thresholds)."""

    key = models.CharField(max_length=80, unique=True)
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=None):
        row = cls.objects.filter(key=key).first()
        return row.value if row else default

    @classmethod
    def put(cls, key, value):
        cls.objects.update_or_create(key=key, defaults={"value": value})


class RecurringPlan(models.Model):
    """One Jobber recurring job, with its detected price/frequency and manual overrides."""

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="recurring_plans")
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="recurring_plan")
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="recurring_plans")
    name = models.CharField(max_length=255, blank=True, default="")
    division = models.CharField(max_length=64, blank=True, default="", db_index=True)
    service = models.CharField(max_length=255, blank=True, default="")

    # Detected from Jobber every night.
    frequency = models.CharField(max_length=64, blank=True, default="")
    visits_per_month = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    visit_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    monthly_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    start_date = models.DateField(null=True, blank=True, db_index=True)
    end_date = models.DateField(null=True, blank=True, db_index=True)
    detected_status = models.CharField(max_length=32, choices=PLAN_STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)
    last_detected_at = models.DateTimeField(null=True, blank=True)

    # Set in Insights by authorized users. Override wins over detection.
    status_override = models.CharField(max_length=32, choices=PLAN_STATUS_CHOICES, blank=True, default="")
    pause_reason = models.CharField(max_length=255, blank=True, default="")
    pause_start = models.DateField(null=True, blank=True)
    expected_return = models.DateField(null=True, blank=True)
    manual_monthly_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    manual_note = models.TextField(blank=True, default="")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["integration", "detected_status"])]

    def __str__(self):
        return self.name or f"Plan {self.pk}"

    @property
    def status(self) -> str:
        return self.status_override or self.detected_status

    @property
    def effective_monthly_value(self) -> Decimal:
        if self.manual_monthly_value is not None:
            return self.manual_monthly_value
        return self.monthly_value or Decimal("0")


class MrrSnapshot(models.Model):
    """End-of-month MRR of a plan. Months before tracking started are rebuilt from visit history."""

    SOURCE_HISTORY = "history"
    SOURCE_TRACKED = "tracked"
    SOURCE_CHOICES = [(SOURCE_HISTORY, "Rebuilt from visit history"), (SOURCE_TRACKED, "Tracked nightly")]

    plan = models.ForeignKey(RecurringPlan, on_delete=models.CASCADE, related_name="snapshots")
    month = models.DateField(db_index=True)  # first day of month
    monthly_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=32, choices=PLAN_STATUS_CHOICES, default=STATUS_ACTIVE)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_HISTORY)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["plan", "month"], name="uniq_mrr_snapshot_plan_month")]
        indexes = [models.Index(fields=["month", "status"])]


class MrrChange(models.Model):
    """A detected or manual change to a plan (price, frequency, status)."""

    TYPE_NEW = "new"
    TYPE_PRICE = "price"
    TYPE_FREQUENCY = "frequency"
    TYPE_STATUS = "status"
    TYPE_CORRECTION = "correction"
    TYPE_CHOICES = [
        (TYPE_NEW, "New plan"),
        (TYPE_PRICE, "Price change"),
        (TYPE_FREQUENCY, "Frequency change"),
        (TYPE_STATUS, "Status change"),
        (TYPE_CORRECTION, "Manual correction"),
    ]

    plan = models.ForeignKey(RecurringPlan, on_delete=models.CASCADE, related_name="changes")
    changed_on = models.DateField(db_index=True)
    change_type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    old_value = models.CharField(max_length=120, blank=True, default="")
    new_value = models.CharField(max_length=120, blank=True, default="")
    monthly_delta = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    note = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_on", "-id"]
