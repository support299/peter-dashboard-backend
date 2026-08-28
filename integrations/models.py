from django.db import models
from django.utils import timezone


class Integration(models.Model):
    """A connected external system (currently Jobber; ready for more sources)."""

    PROVIDER_JOBBER = "jobber"
    PROVIDER_CHOICES = [(PROVIDER_JOBBER, "Jobber")]

    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_ERROR = "error"
    STATUS_DISCONNECTED = "disconnected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_ERROR, "Error"),
        (STATUS_DISCONNECTED, "Disconnected"),
    ]

    provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES, default=PROVIDER_JOBBER)
    account_external_id = models.CharField(max_length=128, blank=True, default="")
    account_name = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    scopes = models.CharField(max_length=500, blank=True, default="")
    access_token = models.TextField(blank=True, default="")
    refresh_token = models.TextField(blank=True, default="")
    token_type = models.CharField(max_length=32, default="Bearer")
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    requested_api_version = models.CharField(max_length=32, blank=True, default="")
    served_api_version = models.CharField(max_length=32, blank=True, default="")
    version_warning = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "account_external_id"],
                condition=~models.Q(account_external_id=""),
                name="uniq_integration_provider_account",
            )
        ]
        indexes = [
            models.Index(fields=["provider", "status"]),
        ]

    def __str__(self):
        return f"{self.provider}:{self.account_name or self.account_external_id or self.pk}"

    @property
    def is_token_fresh(self):
        if not self.access_token or not self.access_token_expires_at:
            return False
        return timezone.now() < self.access_token_expires_at


class OAuthState(models.Model):
    """Short-lived PKCE + CSRF state for the Jobber authorization code flow."""

    state = models.CharField(max_length=128, unique=True)
    code_verifier = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["created_at"])]


class SyncRun(models.Model):
    KIND_FULL = "full"
    KIND_INCREMENTAL = "incremental"
    KIND_CHOICES = [(KIND_FULL, "Full"), (KIND_INCREMENTAL, "Incremental")]

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

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="sync_runs")
    kind = models.CharField(max_length=24, choices=KIND_CHOICES, default=KIND_FULL)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    entity_counts = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["integration", "status"])]


class SchemaSnapshot(models.Model):
    """Introspection of selected Jobber GraphQL types so we can study live shape."""

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="schema_snapshots")
    api_version = models.CharField(max_length=32)
    served_api_version = models.CharField(max_length=32, blank=True, default="")
    query_fields = models.JSONField(default=list, blank=True)
    types = models.JSONField(default=dict, blank=True)
    deprecated_fields = models.JSONField(default=list, blank=True)
    versioning = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class WebhookEvent(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_PROCESSED = "processed"
    STATUS_FAILED = "failed"
    STATUS_IGNORED = "ignored"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_PROCESSED, "Processed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_IGNORED, "Ignored"),
    ]

    integration = models.ForeignKey(
        Integration, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_events"
    )
    event_key = models.CharField(max_length=64, unique=True)
    topic = models.CharField(max_length=80, db_index=True)
    account_id = models.CharField(max_length=160, db_index=True)
    item_id = models.CharField(max_length=160, db_index=True)
    app_id = models.CharField(max_length=80, blank=True, default="")
    occurred_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    error_message = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["topic", "item_id", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.topic} {self.item_id}"
