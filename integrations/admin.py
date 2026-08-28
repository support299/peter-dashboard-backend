from django.contrib import admin

from integrations.models import Integration, OAuthState, SchemaSnapshot, SyncRun, WebhookEvent


@admin.register(Integration)
class IntegrationAdmin(admin.ModelAdmin):
    list_display = ("provider", "account_name", "status", "requested_api_version", "served_api_version", "last_synced_at")
    readonly_fields = (
        "access_token",
        "refresh_token",
        "requested_api_version",
        "served_api_version",
        "version_warning",
        "created_at",
        "updated_at",
    )


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("id", "integration", "kind", "status", "started_at", "finished_at")
    readonly_fields = ("entity_counts", "error_message", "created_at")


@admin.register(SchemaSnapshot)
class SchemaSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "integration", "api_version", "served_api_version", "created_at")
    readonly_fields = ("query_fields", "types", "deprecated_fields", "versioning", "created_at")


@admin.register(OAuthState)
class OAuthStateAdmin(admin.ModelAdmin):
    list_display = ("state", "created_at", "used_at")


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("topic", "item_id", "status", "occurred_at", "created_at")
    list_filter = ("topic", "status")
    search_fields = ("item_id", "account_id", "topic")
    readonly_fields = ("event_key", "payload", "created_at", "processed_at")
