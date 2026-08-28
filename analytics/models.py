from django.db import models

from analytics.hub_models import (  # noqa: F401
    AdminInternalSyncRun,
    HubAlert,
    HubBonus,
    HubEmployee,
    HubLeaveRequest,
    HubPendingLockIn,
    HubVisit,
)
from analytics.pricing_models import (  # noqa: F401
    PricingAddon,
    PricingBundle,
    PricingCoupon,
    PricingLocation,
    PricingPackage,
    PricingService,
    PricingSubmission,
    PricingSyncRun,
)
from integrations.models import Integration


class Dashboard(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default="")
    layout = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Widget(models.Model):
    KPI_CARD = "kpi_card"
    TIMESERIES = "timeseries"
    TABLE = "table"
    BREAKDOWN = "breakdown"
    TYPE_CHOICES = [
        (KPI_CARD, "KPI card"),
        (TIMESERIES, "Timeseries"),
        (TABLE, "Table"),
        (BREAKDOWN, "Breakdown"),
    ]

    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="widgets")
    slug = models.SlugField()
    title = models.CharField(max_length=160)
    widget_type = models.CharField(max_length=32, choices=TYPE_CHOICES, default=KPI_CARD)
    kpi_keys = models.JSONField(default=list, blank=True)
    config = models.JSONField(default=dict, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("dashboard", "slug")]
        ordering = ["sort_order", "id"]


class KpiDefinition(models.Model):
    UNIT_COUNT = "count"
    UNIT_CURRENCY = "currency"
    UNIT_PERCENT = "percent"
    UNIT_CHOICES = [
        (UNIT_COUNT, "Count"),
        (UNIT_CURRENCY, "Currency"),
        (UNIT_PERCENT, "Percent"),
    ]

    AGG_COUNT = "count"
    AGG_SUM = "sum"
    AGG_AVG = "avg"
    AGG_CHOICES = [(AGG_COUNT, "Count"), (AGG_SUM, "Sum"), (AGG_AVG, "Average")]

    key = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=64, blank=True, default="")
    unit = models.CharField(max_length=16, choices=UNIT_CHOICES, default=UNIT_COUNT)
    source_model = models.CharField(max_length=64)
    aggregation = models.CharField(max_length=16, choices=AGG_CHOICES, default=AGG_COUNT)
    date_field = models.CharField(max_length=64, default="start_at")
    value_field = models.CharField(max_length=64, blank=True, default="")
    filters = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class MetricFact(models.Model):
    """
    Daily facts keyed by KPI + dimensions.

    New dashboards/KPIs do not require new columns — add a KpiDefinition
    and facts for that key.
    """

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="metric_facts")
    kpi_key = models.CharField(max_length=80, db_index=True)
    bucket_date = models.DateField(db_index=True)
    dimensions = models.JSONField(default=dict, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "kpi_key", "bucket_date", "dimensions"],
                name="uniq_metric_fact",
            )
        ]
        indexes = [
            models.Index(fields=["kpi_key", "bucket_date"]),
            models.Index(fields=["integration", "bucket_date"]),
        ]
