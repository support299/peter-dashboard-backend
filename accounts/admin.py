from django.contrib import admin

from accounts.models import PasswordResetToken, RefreshToken


@admin.register(RefreshToken)
class RefreshTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at", "expires_at", "revoked_at", "ip_address")
    list_filter = ("revoked_at",)
    search_fields = ("user__username", "user__email", "token_hash")
    readonly_fields = ("token_hash", "created_at")


@admin.register(PasswordResetToken)
class PasswordResetTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at", "expires_at", "used_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("token_hash", "created_at")
