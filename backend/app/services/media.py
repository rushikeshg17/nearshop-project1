"""Image uploads behind a small storage interface.

Uploads are decoded with Pillow, so anything that is not a real image is rejected, then re-encoded to
WebP (this also strips EXIF/GPS metadata) and resized to a sane maximum. The result is stored under a
key of the form folder/<uuid hex>.webp, which is what image_path holds in the database.

NEARSHOP_MEDIA_BACKEND picks where the bytes go:
- local: files under settings.media_dir, served by the API at /media (development and test);
- s3: an S3-compatible bucket such as Cloudflare R2 (staging and production), served from the bucket's
  public domain, NEARSHOP_MEDIA_PUBLIC_BASE_URL. The API's disk on Render is lost on every redeploy.
"""
import io
import logging
import uuid
from pathlib import Path
from typing import Protocol

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, settings
from app.core.errors import AppError

log = logging.getLogger("nearshop.media")

MAX_SIDE = 1400
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
CONTENT_TYPE = "image/webp"
# Every key holds a fresh uuid and is never rewritten, so browsers and the CDN may keep it for a year.
CACHE_CONTROL = "public, max-age=31536000, immutable"


class MediaStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def delete(self, key: str) -> None: ...


class LocalStorage:
    """Files on the API's own disk: development and test only."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path | None:
        path = (self.root / key).resolve()
        return path if self.root.resolve() in path.parents else None

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        if path is None:
            raise ValueError("media key escapes the media directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path is not None and path.exists():
            path.unlink()


class S3Storage:
    """An S3-compatible bucket (Cloudflare R2 in staging and production)."""

    def __init__(self, bucket: str, client):
        self.bucket = bucket
        self.client = client

    @classmethod
    def from_settings(cls, cfg: Settings) -> "S3Storage":
        import boto3  # imported only when this backend is selected, so development and tests never load it
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=cfg.media_s3_endpoint,
            aws_access_key_id=cfg.media_s3_access_key_id,
            aws_secret_access_key=cfg.media_s3_secret_access_key.get_secret_value(),
            region_name=cfg.media_s3_region,
            # Send checksums only where S3 requires them; R2 and other S3-compatible stores differ on the rest.
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                          connect_timeout=5, read_timeout=20,
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )
        return cls(cfg.media_s3_bucket, client)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                               CacheControl=CACHE_CONTROL)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def build_storage(cfg: Settings) -> MediaStorage:
    if cfg.media_backend == "s3":
        return S3Storage.from_settings(cfg)
    return LocalStorage(cfg.media_dir)


_storage: MediaStorage | None = None


def get_storage() -> MediaStorage:
    global _storage
    if _storage is None:
        _storage = build_storage(settings)
    return _storage


def use_storage(storage: MediaStorage | None) -> MediaStorage | None:
    """Replace the storage backend (tests use an in-memory fake); None rebuilds it from settings on next use.
    Returns the backend that was in use."""
    global _storage
    previous, _storage = _storage, storage
    return previous


async def save_image(upload: UploadFile, folder: str) -> str:
    data = await upload.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise AppError(f"Images must be under {settings.max_upload_mb} MB", code="file_too_large")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ALLOWED_FORMATS:
            raise AppError("Upload a JPG, PNG or WebP image", code="bad_image")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise AppError("That file is not a readable image", code="bad_image") from None
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    key = f"{folder}/{uuid.uuid4().hex}.webp"
    out = io.BytesIO()
    img.save(out, "WEBP", quality=82, method=5)
    # A bucket upload is a network call; keep it off the event loop.
    await run_in_threadpool(get_storage().put, key, out.getvalue(), CONTENT_TYPE)
    return key


def delete_image(key: str | None) -> None:
    """Remove a stored image. A failure is logged and never reaches the user: the new image is already
    saved, and an orphaned file only costs storage."""
    if not key:
        return
    try:
        get_storage().delete(key)
    except Exception as exc:
        log.warning("could not delete media %s: %s", key, type(exc).__name__)
