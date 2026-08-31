from datetime import datetime, timedelta, timezone as dt_timezone

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model

User = get_user_model()


def _secret() -> str:
    return settings.SECRET_KEY


def access_token_lifetime() -> timedelta:
    minutes = int(getattr(settings, "JWT_ACCESS_TOKEN_MINUTES", 15))
    return timedelta(minutes=minutes)


def refresh_token_lifetime_days() -> int:
    return int(getattr(settings, "JWT_REFRESH_TOKEN_DAYS", 30))


def issue_access_token(user) -> str:
    """Minimal access JWT — subject + times only (no sensitive claims)."""
    now = datetime.now(dt_timezone.utc)
    payload = {
        "typ": "access",
        "sub": str(user.pk),
        "iat": now,
        "exp": now + access_token_lifetime(),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def user_from_access_token(token: str):
    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise jwt.InvalidTokenError("Missing subject")
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if not user:
        raise jwt.InvalidTokenError("User not found")
    return user
