"""Receipt and invoice files in a private S3 bucket.

Files are never public: the API hands out links that expire after a few minutes.
"""

from __future__ import annotations

import re
import uuid

from django.conf import settings
from django.utils import timezone

MAX_BYTES = 10 * 1024 * 1024
LINK_SECONDS = 300

# Extension → content type. HEIC is what iPhones save photos as.
ALLOWED = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "heic": "image/heic",
    "heif": "image/heif",
}


class ReceiptError(ValueError):
    pass


def configured() -> bool:
    return bool(settings.AWS_STORAGE_BUCKET_NAME)


def _client():
    import boto3
    from botocore.config import Config

    region = settings.AWS_S3_REGION_NAME
    # Sign against the regional address: links signed for the global one get redirected and rejected (403).
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def _looks_like(ext: str, head: bytes) -> bool:
    """Check the file really is what its name says (first bytes)."""
    if ext == "pdf":
        return head.startswith(b"%PDF")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8")
    if ext == "png":
        return head.startswith(b"\x89PNG")
    if ext == "gif":
        return head.startswith(b"GIF8")
    if ext == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext in ("heic", "heif"):
        return head[4:8] == b"ftyp"
    return False


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return name[-120:] or "receipt"


def save(upload, prefix: str = "receipts") -> dict:
    """Upload a Django UploadedFile; returns the receipt_* fields to store on the record."""
    if not configured():
        raise ReceiptError("Receipt storage is not set up on the server yet.")
    if upload.size > MAX_BYTES:
        raise ReceiptError("The file is larger than 10 MB.")
    ext = (upload.name.rsplit(".", 1)[-1] if "." in upload.name else "").lower()
    if ext not in ALLOWED:
        raise ReceiptError("Attach a PDF or a photo (JPG, PNG, HEIC or WEBP).")
    head = upload.read(16)
    upload.seek(0)
    if not _looks_like(ext, head):
        raise ReceiptError("This file doesn't look like a real PDF or photo.")
    now = timezone.localdate()
    key = f"{prefix}/{now:%Y/%m}/{uuid.uuid4().hex}-{_safe_name(upload.name)}"
    _client().upload_fileobj(
        upload,
        settings.AWS_STORAGE_BUCKET_NAME,
        key,
        ExtraArgs={"ContentType": ALLOWED[ext], "ServerSideEncryption": "AES256"},
    )
    return {"receipt_key": key, "receipt_name": upload.name[:255], "receipt_type": ALLOWED[ext], "receipt_size": upload.size}


def link(key: str, name: str = "", download: bool = False) -> str:
    disposition = "attachment" if download else "inline"
    safe = _safe_name(name or key.rsplit("/", 1)[-1])
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
            "Key": key,
            "ResponseContentDisposition": f'{disposition}; filename="{safe}"',
        },
        ExpiresIn=LINK_SECONDS,
    )


def delete(key: str) -> None:
    if key and configured():
        try:
            _client().delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
        except Exception:  # A leftover file is harmless; never block the user's delete on it.
            pass


EMPTY = {"receipt_key": "", "receipt_name": "", "receipt_type": "", "receipt_size": 0}
