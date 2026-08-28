from django.db import models


class PricingSyncRun(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    source_generated_at = models.DateTimeField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"PricingSyncRun({self.status})"


class PricingSubmission(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_deleted = models.BooleanField(default=False)

    customer_first_name = models.CharField(max_length=255, blank=True, default="")
    customer_last_name = models.CharField(max_length=255, blank=True, default="")
    customer_company = models.CharField(max_length=255, blank=True, default="")
    customer_email = models.CharField(max_length=255, blank=True, default="", db_index=True)
    customer_phone = models.CharField(max_length=64, blank=True, default="")
    postal_code = models.CharField(max_length=32, blank=True, default="")
    street_address = models.CharField(max_length=500, blank=True, default="")
    heard_about_us = models.CharField(max_length=255, blank=True, default="")
    is_previous_customer = models.BooleanField(default=False)

    property_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    property_name = models.CharField(max_length=255, blank=True, default="")
    num_floors = models.CharField(max_length=64, blank=True, default="")
    actual_sqft = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    size_range = models.CharField(max_length=120, blank=True, default="")

    location_external_id = models.CharField(max_length=64, blank=True, default="")
    location_name = models.CharField(max_length=255, blank=True, default="", db_index=True)

    total_base_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_adjustments = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_surcharges = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_addons_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    discounted_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    bundle_discount_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    final_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, db_index=True)

    coupon_applied = models.BooleanField(default=False)
    coupon_code = models.CharField(max_length=120, blank=True, default="")
    bundle_applied = models.BooleanField(default=False)
    bundle_name = models.CharField(max_length=255, blank=True, default="")
    is_bid_in_person = models.BooleanField(default=False)
    is_on_the_go = models.BooleanField(default=False)

    service_names = models.JSONField(default=list, blank=True)
    quote_url = models.CharField(max_length=1000, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-source_created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "property_type"]),
            models.Index(fields=["location_name", "status"]),
        ]

    @property
    def customer_name(self):
        full = " ".join(part for part in [self.customer_first_name, self.customer_last_name] if part).strip()
        return full or self.customer_company or self.customer_email or self.external_id

    def __str__(self):
        return f"{self.customer_name} · {self.status}"


class PricingService(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    is_commercial = models.BooleanField(default=False)
    is_residential = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    icon_url = models.CharField(max_length=1000, blank=True, default="")
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name or self.external_id


class PricingPackage(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    service_external_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    service_name = models.CharField(max_length=255, blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")
    base_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service_name", "sort_order", "name"]

    def __str__(self):
        return f"{self.service_name} · {self.name}"


class PricingLocation(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=500, blank=True, default="")
    latitude = models.CharField(max_length=64, blank=True, default="")
    longitude = models.CharField(max_length=64, blank=True, default="")
    trip_surcharge = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name or self.external_id


class PricingAddon(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    base_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_global = models.BooleanField(default=False)
    service_ids = models.JSONField(default=list, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name or self.external_id


class PricingCoupon(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    code = models.CharField(max_length=120, blank=True, default="", db_index=True)
    percentage_discount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    fixed_discount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    expiration_date = models.DateTimeField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    is_global = models.BooleanField(default=False)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-used_count", "code"]

    def __str__(self):
        return self.code or self.external_id


class PricingBundle(models.Model):
    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    discount_type = models.CharField(max_length=64, blank=True, default="")
    discount_percentage = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    discount_fixed = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    service_ids = models.JSONField(default=list, blank=True)
    source_payload = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name or self.external_id
