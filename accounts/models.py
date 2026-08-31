import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RefreshToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="refresh_tokens")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replaces",
    )
    user_agent = models.CharField(max_length=255, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "revoked_at"])]

    @classmethod
    def issue(cls, user, *, days: int, user_agent: str = "", ip_address=None) -> tuple["RefreshToken", str]:
        raw = secrets.token_urlsafe(48)
        row = cls.objects.create(
            user=user,
            token_hash=_hash_token(raw),
            expires_at=timezone.now() + timedelta(days=days),
            user_agent=(user_agent or "")[:255],
            ip_address=ip_address,
        )
        return row, raw

    @classmethod
    def lookup(cls, raw: str) -> "RefreshToken | None":
        if not raw:
            return None
        return cls.objects.select_related("user").filter(token_hash=_hash_token(raw)).first()

    @property
    def is_active(self) -> bool:
        if self.revoked_at:
            return False
        return timezone.now() < self.expires_at

    def revoke(self, replaced_by=None):
        self.revoked_at = timezone.now()
        if replaced_by:
            self.replaced_by = replaced_by
        self.save(update_fields=["revoked_at", "replaced_by"])

    @classmethod
    def revoke_all_for_user(cls, user):
        cls.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())

    @classmethod
    def rotate(cls, raw: str, *, user_agent: str = "", ip_address=None) -> tuple["RefreshToken", str, object]:
        """
        Validate refresh token, revoke it, issue a new one (rotation).
        Reuse of an already-rotated token revokes the whole family (theft signal).
        Returns (new_row, new_raw, user).
        """
        from django.db import transaction

        row = cls.lookup(raw)
        if row is None:
            raise ValueError("invalid")

        if row.revoked_at is not None:
            # Possible theft: replay of a rotated refresh token.
            cls.revoke_all_for_user(row.user)
            raise ValueError("reuse")

        if timezone.now() >= row.expires_at:
            row.revoke()
            raise ValueError("expired")

        user = row.user
        if not user.is_active:
            row.revoke()
            raise ValueError("inactive")

        days = int(getattr(settings, "JWT_REFRESH_TOKEN_DAYS", 30))
        try:
            with transaction.atomic():
                new_row, new_raw = cls.issue(
                    user,
                    days=days,
                    user_agent=user_agent,
                    ip_address=ip_address,
                )
                locked = cls.objects.select_for_update().get(pk=row.pk)
                if locked.revoked_at is not None:
                    raise ValueError("reuse")
                locked.revoke(replaced_by=new_row)
        except ValueError as exc:
            if str(exc) == "reuse":
                # Family revoke must happen outside the rolled-back atomic block.
                cls.revoke_all_for_user(user)
            raise

        return new_row, new_raw, user


class PasswordResetToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def issue(cls, user, *, hours: int = 1) -> tuple["PasswordResetToken", str]:
        raw = secrets.token_urlsafe(32)
        row = cls.objects.create(
            user=user,
            token_hash=_hash_token(raw),
            expires_at=timezone.now() + timedelta(hours=hours),
        )
        return row, raw

    @classmethod
    def lookup(cls, raw: str) -> "PasswordResetToken | None":
        if not raw:
            return None
        return cls.objects.select_related("user").filter(token_hash=_hash_token(raw)).first()

    @property
    def is_valid(self) -> bool:
        if self.used_at:
            return False
        return timezone.now() < self.expires_at

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])
