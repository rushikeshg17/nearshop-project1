"""Photos behind the storage interface (app/services/media.py): an in-memory fake stands in for the
bucket, so no test touches a real one; the local backend keeps today's behaviour. Every photo is stored with
its thumbnail (P2.7, tests/test_upload_safety.py); photos() leaves the thumbnails out."""
import asyncio
import io
import logging
import re

import pytest
from PIL import Image
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.main import serves_local_media
from app.services import media
from app.services.media import CACHE_CONTROL, LocalStorage, S3Storage, build_storage, delete_image, thumbnail_key
from tests.conftest import HEADERS

KEY = re.compile(r"^(products|shops)/[0-9a-f]{32}\.webp$")


class FakeStorage:
    """An in-memory bucket: key -> (bytes, content type)."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key, data, content_type):
        self.objects[key] = (data, content_type)

    def delete(self, key):
        self.objects.pop(key, None)

    def get(self, key):
        return self.objects[key][0] if key in self.objects else None

    def size(self, key):
        return len(self.objects[key][0]) if key in self.objects else None


def photos(storage: FakeStorage) -> list[str]:
    """The stored photo keys, without their thumbnails."""
    return [k for k in storage.objects if not k.endswith(media.THUMB_SUFFIX + ".webp")]


class BrokenDeleteStorage(FakeStorage):
    def delete(self, key):
        raise ConnectionError("bucket unreachable")


class FakeS3Client:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        if kwargs["Key"].startswith("missing/"):
            raise _client_error("NoSuchKey")
        return {"Body": io.BytesIO(b"bytes")}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if kwargs["Key"].startswith("missing/"):
            raise _client_error("404")
        if kwargs["Key"].startswith("unlisted/"):
            raise _client_error("403")  # AWS S3, for a missing key, to credentials without s3:ListBucket
        if kwargs["Key"].startswith("broken/"):
            raise _client_error("500")
        return {"ContentLength": 5}


def _client_error(code: str) -> Exception:
    error = Exception(code)
    error.response = {"Error": {"Code": code}}  # what botocore's ClientError carries
    return error


def png(size=(1800, 900), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def fake_storage():
    fake = FakeStorage()
    previous = media.use_storage(fake)
    yield fake
    media.use_storage(previous)


@pytest.fixture
def local_storage():
    # The local backend in settings.media_dir (a temporary directory in tests), the same directory the API
    # mounts at /media. Built explicitly, never from settings, so no configuration can point it at a bucket.
    previous = media.use_storage(LocalStorage(settings.media_dir))
    yield settings.media_dir
    media.use_storage(previous)


def _upload_product_image(owner, product_id, data=None, name="photo.png", content_type="image/png"):
    return owner.post(f"/api/owner/products/{product_id}/image", headers=HEADERS,
                      files={"file": (name, data if data is not None else png(), content_type)})


# ------------------------------------------------------------------ fake bucket


def test_upload_stores_webp_under_expected_key_and_delete_removes_it(owner_a, ids, fake_storage):
    res = _upload_product_image(owner_a, ids["charger"])
    assert res.status_code == 200, res.text
    [key] = photos(fake_storage)
    data, content_type = fake_storage.objects[key]
    assert set(fake_storage.objects) == {key, thumbnail_key(key)}
    assert KEY.match(key) and key.startswith("products/")
    assert content_type == "image/webp"
    img = Image.open(io.BytesIO(data))
    assert img.format == "WEBP" and max(img.size) == media.MAX_SIDE  # re-encoded and resized as before
    assert res.json()["image_url"] == f"/media/{key}"

    # A second upload replaces the first and deletes the old object.
    res = _upload_product_image(owner_a, ids["charger"], png(color=(10, 200, 10)))
    assert res.status_code == 200, res.text
    assert key not in fake_storage.objects and thumbnail_key(key) not in fake_storage.objects
    [new_key] = photos(fake_storage)
    assert KEY.match(new_key) and new_key != key

    delete_image(new_key)
    assert fake_storage.objects == {}


def test_shop_image_response_uses_public_base_url(owner_a, fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "media_public_base_url", "https://media.example.test")
    res = owner_a.post("/api/owner/shop/image", headers=HEADERS, files={"file": ("shop.png", png(), "image/png")})
    assert res.status_code == 200, res.text
    [key] = photos(fake_storage)
    assert key.startswith("shops/")
    assert res.json()["image_url"] == f"https://media.example.test/{key}"
    shop = owner_a.get("/api/owner/shop", headers=HEADERS).json()
    assert shop["image_url"] == f"https://media.example.test/{key}"


def test_non_images_are_still_rejected(owner_a, ids, fake_storage):
    res = _upload_product_image(owner_a, ids["tape"], b"%PDF-1.4 not an image", "file.pdf", "application/pdf")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_image"
    buf = io.BytesIO()
    Image.new("RGB", (20, 20)).save(buf, "BMP")  # a real image, but not an allowed format
    res = _upload_product_image(owner_a, ids["tape"], buf.getvalue(), "photo.bmp", "image/bmp")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_image"
    assert fake_storage.objects == {}


def test_delete_failure_is_logged_not_raised(caplog):
    previous = media.use_storage(BrokenDeleteStorage())
    try:
        with caplog.at_level(logging.WARNING, logger="nearshop.media"):
            delete_image("products/" + "a" * 32 + ".webp")
    finally:
        media.use_storage(previous)
    assert "could not delete media" in caplog.text and "ConnectionError" in caplog.text


def test_upload_succeeds_when_old_image_cannot_be_deleted(owner_a, ids):
    broken = BrokenDeleteStorage()
    previous = media.use_storage(broken)
    try:
        assert _upload_product_image(owner_a, ids["tape"]).status_code == 200
        res = _upload_product_image(owner_a, ids["tape"])
    finally:
        media.use_storage(previous)
    assert res.status_code == 200, res.text
    assert len(photos(broken)) == 2  # the old object is orphaned, not an error


# ------------------------------------------------------------------ S3 backend (fake client, no bucket)


def test_s3_backend_puts_webp_with_immutable_cache_header():
    client = FakeS3Client()
    storage = S3Storage("photos", client)
    storage.put("products/abc.webp", b"bytes", "image/webp")
    storage.delete("products/abc.webp")
    assert client.calls == [
        ("put_object", {"Bucket": "photos", "Key": "products/abc.webp", "Body": b"bytes",
                        "ContentType": "image/webp", "CacheControl": CACHE_CONTROL}),
        ("delete_object", {"Bucket": "photos", "Key": "products/abc.webp"}),
    ]
    # Reads for the thumbnail backfill: a missing key is None, never an error.
    assert storage.get("products/abc.webp") == b"bytes" and storage.size("products/abc.webp") == 5
    assert storage.get("missing/abc.webp") is None and storage.size("missing/abc.webp") is None
    assert CACHE_CONTROL == "public, max-age=31536000, immutable"


def test_s3_size_treats_403_as_missing_so_the_backfill_works_without_list_permission():
    storage = S3Storage("photos", FakeS3Client())
    assert storage.size("unlisted/abc.thumb.webp") is None
    with pytest.raises(Exception, match="500"):
        storage.size("broken/abc.thumb.webp")  # any other error still surfaces


def _s3_settings(**overrides) -> Settings:
    values = dict(environment="test", media_backend="s3", media_s3_endpoint="https://acct.r2.example.test",
                  media_s3_bucket="photos", media_s3_access_key_id="test-key-id",
                  media_s3_secret_access_key="test-secret", media_public_base_url="https://media.example.test/")
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_settings_select_the_backend():
    cfg = _s3_settings()
    assert cfg.media_public_base_url == "https://media.example.test"  # trailing slash dropped
    storage = build_storage(cfg)  # builds a client; nothing is sent
    assert isinstance(storage, S3Storage) and storage.bucket == "photos"
    assert isinstance(build_storage(Settings(_env_file=None, environment="test")), LocalStorage)
    assert Settings(_env_file=None, environment="test", media_backend="").media_backend == "local"


@pytest.mark.parametrize("missing", ["media_s3_endpoint", "media_s3_bucket", "media_s3_access_key_id",
                                     "media_s3_secret_access_key", "media_public_base_url"])
def test_s3_backend_requires_every_setting(missing):
    with pytest.raises(ValidationError) as err:
        _s3_settings(**{missing: ""})
    assert "NEARSHOP_" + missing.upper() in str(err.value)
    assert "test-secret" not in str(err.value)


def test_bad_backend_or_base_url_is_refused():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", media_backend="disk")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", media_public_base_url="media.example.test")


@pytest.mark.parametrize("environment", ["staging", "production", "demo"])
def test_deployed_environments_need_an_https_base_url(environment):
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None, environment=environment, media_public_base_url="http://media.example.test")
    assert "https://" in str(err.value)
    cfg = Settings(_env_file=None, environment=environment, media_public_base_url="https://media.example.test")
    assert cfg.media_public_base_url == "https://media.example.test"


@pytest.mark.parametrize("environment", ["development", "test"])
def test_http_base_url_is_accepted_in_development_and_test(environment):
    cfg = Settings(_env_file=None, environment=environment, media_public_base_url="http://127.0.0.1:9000/photos")
    assert cfg.media_public_base_url == "http://127.0.0.1:9000/photos"


def test_first_upload_builds_the_storage_off_the_event_loop(owner_a, ids, monkeypatch):
    # With s3 the first upload imports boto3 and builds the client; that must happen in a worker thread.
    built: list[bool] = []
    fake = FakeStorage()

    def build(cfg):
        try:
            asyncio.get_running_loop()
            built.append(True)
        except RuntimeError:
            built.append(False)
        return fake

    monkeypatch.setattr(media, "build_storage", build)
    previous = media.use_storage(None)
    try:
        assert _upload_product_image(owner_a, ids["tape"]).status_code == 200
    finally:
        media.use_storage(previous)
    assert built == [False] and len(photos(fake)) == 1


# ------------------------------------------------------------------ serializers


def test_media_url_is_absolute_when_a_base_url_is_set(monkeypatch, client, ids):
    from app.schemas.serializers import media_url

    assert media_url(None) is None and media_url("") is None
    assert media_url("products/abc.webp") == "/media/products/abc.webp"
    monkeypatch.setattr(settings, "media_public_base_url", "https://media.example.test")
    assert media_url("products/abc.webp") == "https://media.example.test/products/abc.webp"


def test_public_pages_use_the_base_url(monkeypatch, owner_a, client, ids, fake_storage):
    assert _upload_product_image(owner_a, ids["charger"]).status_code == 200
    [key] = photos(fake_storage)
    monkeypatch.setattr(settings, "media_public_base_url", "https://media.example.test")
    product = client.get(f"/api/products/{ids['charger']}", headers=HEADERS)
    assert product.status_code == 200, product.text
    assert product.json()["image_url"] == f"https://media.example.test/{key}"


# ------------------------------------------------------------------ local backend


def test_local_backend_behaves_as_before(owner_a, client, ids, local_storage):
    res = _upload_product_image(owner_a, ids["tape"])
    assert res.status_code == 200, res.text
    url = res.json()["image_url"]
    key = url.removeprefix("/media/")
    assert url.startswith("/media/") and KEY.match(key)
    path = local_storage / key
    assert path.is_file() and Image.open(path).format == "WEBP"
    served = client.get(url)  # the API serves /media itself in development and test
    assert served.status_code == 200 and served.content == path.read_bytes()

    delete_image(key)
    assert not path.exists()
    delete_image(key)  # already gone: no error
    delete_image(None)


def test_local_backend_never_leaves_its_directory(tmp_path):
    root = tmp_path / "media"
    outside = tmp_path / "secret.txt"
    outside.write_text("keep")
    storage = LocalStorage(root)
    storage.delete("../secret.txt")
    assert outside.exists()
    with pytest.raises(ValueError):
        storage.put("../escape.webp", b"x", "image/webp")


def test_media_is_mounted_only_where_the_api_holds_the_photos():
    assert serves_local_media("development", "local") and serves_local_media("test", "local")
    assert serves_local_media("development", "s3") and serves_local_media("test", "s3")
    assert not serves_local_media("staging", "s3") and not serves_local_media("production", "s3")
    # Until a deployed environment has its bucket it keeps today's local disk (startup warns).
    assert serves_local_media("production", "local")
