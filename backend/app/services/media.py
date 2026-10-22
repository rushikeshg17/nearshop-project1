"""Image uploads behind a small storage interface.

Uploads are checked from their header (at most settings.max_image_pixels and settings.max_image_side px a
side, before any decoding), decoded with Pillow (first frame only), so anything that is not a real image is
rejected, then re-encoded to WebP (this also strips EXIF/GPS metadata) at up to 1,400 px, plus a 320 px
thumbnail. The photo is stored under a key of the form folder/<uuid hex>.webp, which is what image_path
holds in the database, and its thumbnail under folder/<uuid hex>.thumb.webp (thumbnail_key). Photos stored
before thumbnails existed get theirs from python -m database.admin_tasks backfill-thumbnails.

NEARSHOP_MEDIA_BACKEND picks where the bytes go:
- local: files under settings.media_dir, served by the API at /media (development and test);
- s3: an S3-compatible bucket such as Cloudflare R2 (staging and production), served from the bucket's
  public domain, NEARSHOP_MEDIA_PUBLIC_BASE_URL. The API's disk on Render is lost on every redeploy.

KYC documents (P3.3) go to a separate, private store with the same interface (private_storage): a directory
nothing serves (NEARSHOP_KYC_MEDIA_DIR) or a bucket with no public access (NEARSHOP_KYC_S3_BUCKET). They pass
the same size, pixel and decoding checks, are re-encoded the same way, and are opened only through links that
expire after SIGNED_LINK_SECONDS: presigned bucket links, or HMAC-signed API links for the local directory.
"""
import hashlib
import hmac
import io
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import Settings, settings
from app.core.errors import AppError

log = logging.getLogger("nearshop.media")

MAX_SIDE = 1400
THUMB_SIDE = 320
THUMB_SUFFIX = ".thumb"
PHOTO_QUALITY = 82
THUMB_QUALITY = 75
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
CONTENT_TYPE = "image/webp"
# Every key holds a fresh uuid and is never rewritten, so browsers and the CDN may keep it for a year.
CACHE_CONTROL = "public, max-age=31536000, immutable"


class MediaStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def get(self, key: str) -> bytes | None:
        """The stored bytes, or None when the key does not exist."""
        ...

    def size(self, key: str) -> int | None:
        """The stored size in bytes, or None when the key does not exist."""
        ...


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

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path is not None and path.is_file() else None

    def size(self, key: str) -> int | None:
        path = self._path(key)
        return path.stat().st_size if path is not None and path.is_file() else None


class PrivateLocalStorage(LocalStorage):
    """KYC documents on the API's own disk, outside media_dir, so the /media mount never serves them."""

    def signed_url(self, key: str, seconds: int) -> str:
        expires = int(time.time()) + seconds
        return f"/api/admin/kyc-files/{quote(key)}?expires={expires}&signature={_sign(key, expires)}"


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

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            if _missing(exc):
                return None
            raise

    def size(self, key: str) -> int | None:
        """None also on 403: AWS S3 answers a HEAD for a missing key with 403 when the credentials lack
        s3:ListBucket, so without this the thumbnail backfill would count every older photo as failed. A
        real permission problem still shows, as a failure on the get or put that follows."""
        try:
            return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except Exception as exc:
            if _missing(exc, forbidden_too=True):
                return None
            raise


class PrivateS3Storage(S3Storage):
    """KYC documents in a bucket with no public access; never cached by anything in between."""

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                               CacheControl="private, no-store")

    def signed_url(self, key: str, seconds: int) -> str:
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                                  ExpiresIn=seconds)


def _missing(exc: Exception, forbidden_too: bool = False) -> bool:
    """Whether a botocore ClientError says the key does not exist (checked by its code, so botocore is
    never imported here). forbidden_too also counts 403 (see S3Storage.size)."""
    code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", ""))
    return code in ("404", "NoSuchKey", "NotFound") or (forbidden_too and code in ("403", "AccessDenied", "Forbidden"))


def build_storage(cfg: Settings) -> MediaStorage:
    if cfg.media_backend == "s3":
        return S3Storage.from_settings(cfg)
    return LocalStorage(cfg.media_dir)


_storage: MediaStorage | None = None
_storage_lock = threading.Lock()


def get_storage() -> MediaStorage:
    """The storage backend, built on first use. It may run in several worker threads at once (uploads and
    deletes both call it off the event loop), so only one of them builds the client."""
    global _storage
    if _storage is None:
        with _storage_lock:
            if _storage is None:
                _storage = build_storage(settings)
    return _storage


def use_storage(storage: MediaStorage | None) -> MediaStorage | None:
    """Replace the storage backend (tests use an in-memory fake); None rebuilds it from settings on next use.
    Returns the backend that was in use."""
    global _storage
    previous, _storage = _storage, storage
    return previous


# ------------------------------------------------------------------ decoding and saving
# Everything below decodes, encodes or calls the storage, so it runs in a worker thread: the upload routes
# are plain def handlers (FastAPI runs those in its thread pool), never on the event loop.


def thumbnail_key(key: str) -> str:
    """Where a photo's thumbnail lives: the same key with a .thumb suffix (products/<hex>.thumb.webp)."""
    stem, dot, ext = key.rpartition(".")
    return f"{stem}{THUMB_SUFFIX}.{ext}" if dot else f"{key}{THUMB_SUFFIX}"


def _too_large() -> AppError:
    return AppError(f"That image is too large: at most {settings.max_image_pixels / 1_000_000:g} megapixels and "
                    f"{settings.max_image_side:,} px on a side", code="bad_image", status_code=422)


def decode_image(data: bytes) -> Image.Image:
    """Decode an uploaded image to RGB, upright, first frame only, after checking its size from the header.

    Image.open reads only the header, so a canvas over the pixel or side limit is refused before a single
    pixel is decoded, and Pillow's own decompression-bomb error (above twice Image.MAX_IMAGE_PIXELS, raised
    by Image.open; not an OSError) gets the same 422. An animated GIF or WebP opens on its first frame, and
    nothing here seeks, so only that frame is decoded."""
    try:
        # Only the allowed formats' readers ever see the bytes; anything else is UnidentifiedImageError.
        img = Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS)
        width, height = img.size
        if width * height > settings.max_image_pixels or max(width, height) > settings.max_image_side:
            raise _too_large()
        if img.format == "JPEG":
            # Let the JPEG decoder scale down by a power of two while decoding (at least twice MAX_SIDE).
            img.draft(img.mode, (2 * MAX_SIDE, 2 * MAX_SIDE))
        return ImageOps.exif_transpose(img).convert("RGB")
    except Image.DecompressionBombError:
        raise _too_large() from None
    except UnidentifiedImageError:
        raise AppError("Upload a JPG, PNG, WebP or GIF image", code="bad_image") from None
    except (OSError, ValueError, SyntaxError, EOFError):
        raise AppError("That file is not a readable image", code="bad_image") from None


def encode_webp(img: Image.Image, side: int, quality: int) -> bytes:
    """Shrink img in place to at most side px on its longest side, and encode it as WebP (no metadata)."""
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.save(out, "WEBP", quality=quality, method=5)
    return out.getvalue()


def make_thumbnail(data: bytes) -> bytes:
    """The thumbnail of a stored photo (thumbnail backfill, database/admin_tasks.py)."""
    return encode_webp(decode_image(data), THUMB_SIDE, THUMB_QUALITY)


def save_image(upload: UploadFile, folder: str) -> str:
    """Check, re-encode and store an uploaded photo and its thumbnail; returns the photo's key. Blocking:
    call it from a plain def handler or a worker thread, never on the event loop."""
    limit = settings.max_upload_mb * 1024 * 1024
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise AppError(f"Images must be under {settings.max_upload_mb} MB", code="file_too_large")
    img = decode_image(data)
    del data
    photo = encode_webp(img, MAX_SIDE, PHOTO_QUALITY)
    thumb = encode_webp(img, THUMB_SIDE, THUMB_QUALITY)  # from the already shrunk image
    key = f"{folder}/{uuid.uuid4().hex}.webp"
    storage = get_storage()
    storage.put(key, photo, CONTENT_TYPE)
    try:
        storage.put(thumbnail_key(key), thumb, CONTENT_TYPE)
    except Exception:
        delete_image(key)  # no photo without its thumbnail
        raise
    return key


def delete_image(key: str | None) -> None:
    """Remove a stored photo and its thumbnail. A failure is logged and never reaches the user: the new
    image is already saved, and an orphaned file only costs storage. A photo stored before thumbnails
    existed has none; deleting a missing key is not an error."""
    if not key:
        return
    for k in (key, thumbnail_key(key)):
        try:
            get_storage().delete(k)
        except Exception as exc:
            log.warning("could not delete media %s: %s", k, type(exc).__name__)


# ------------------------------------------------------------------ private KYC documents (P3.3)

SIGNED_LINK_SECONDS = 300  # admins' links to KYC documents work for 5 minutes
PRIVATE_PREFIX = "kyc/"


class PrivateStorageUnavailable(AppError):
    status_code = 503
    code = "kyc_storage_unavailable"


def build_private_storage(cfg: Settings):
    """The KYC document store, or None when the environment has none: an s3 deployment without its own
    private bucket (a bucket shared with public photos would publish the documents)."""
    if cfg.media_backend == "s3":
        bucket = cfg.kyc_s3_bucket.strip()
        if not bucket or bucket == cfg.media_s3_bucket.strip():
            return None
        public = S3Storage.from_settings(cfg)
        return PrivateS3Storage(bucket, public.client)
    return PrivateLocalStorage(cfg.kyc_media_dir)


_private_storage = None
_private_built = False


def private_storage():
    """The KYC document store (built on first use), or PrivateStorageUnavailable (503)."""
    global _private_storage, _private_built
    if not _private_built:
        with _storage_lock:
            if not _private_built:
                _private_storage, _private_built = build_private_storage(settings), True
    if _private_storage is None:
        raise PrivateStorageUnavailable("Document uploads are not set up yet. Please try again later.")
    return _private_storage


def use_private_storage(storage) -> object:
    """Replace the KYC document store (tests use an in-memory fake with signed_url); None rebuilds it from
    settings on next use. Returns the store that was in use."""
    global _private_storage, _private_built
    previous = _private_storage
    _private_storage, _private_built = storage, storage is not None
    return previous


def _sign(key: str, expires: int) -> str:
    message = f"kyc-file:{key}:{expires}".encode()
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()


def check_signed_link(key: str, expires: int, signature: str, now: float | None = None) -> bool:
    """Whether a local signed KYC link is genuine and unexpired (now defaults to the current time)."""
    if not key.startswith(PRIVATE_PREFIX) or (now if now is not None else time.time()) > expires:
        return False
    return hmac.compare_digest(_sign(key, expires), signature or "")


def signed_link(key: str | None, seconds: int = SIGNED_LINK_SECONDS) -> str | None:
    """A link to a private KYC document that works for `seconds` (5 minutes by default)."""
    return private_storage().signed_url(key, seconds) if key else None


def read_private(key: str) -> bytes | None:
    return private_storage().get(key) if key.startswith(PRIVATE_PREFIX) else None


def save_private_image(upload: UploadFile, kind: str) -> str:
    """Check and re-encode a KYC photo like any upload (size, pixel and decoding rules; metadata stripped),
    and store it privately, without a thumbnail. Returns its key, kyc/<kind>/<uuid hex>.webp. Blocking."""
    store = private_storage()
    limit = settings.max_upload_mb * 1024 * 1024
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise AppError(f"Images must be under {settings.max_upload_mb} MB", code="file_too_large")
    img = decode_image(data)
    del data
    key = f"{PRIVATE_PREFIX}{kind}/{uuid.uuid4().hex}.webp"
    store.put(key, encode_webp(img, MAX_SIDE, PHOTO_QUALITY), CONTENT_TYPE)
    return key


def delete_private(key: str | None) -> None:
    """Remove a KYC document; a failure is logged, never shown (as delete_image)."""
    if not key:
        return
    try:
        private_storage().delete(key)
    except Exception as exc:
        log.warning("could not delete private media %s: %s", key, type(exc).__name__)
