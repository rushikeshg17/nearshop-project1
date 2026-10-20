"""Photos behind the storage interface (app/services/media.py): an in-memory fake stands in for the
bucket, so no test touches a real one; the local backend keeps today's behaviour."""
import io
import logging
import re

import pytest
from PIL import Image
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.main import serves_local_media
from app.services import media
from app.services.media import CACHE_CONTROL, LocalStorage, S3Storage, build_storage, delete_image
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
    # Rebuilt from settings: the local backend in settings.media_dir (a temporary directory in tests), the
    # same directory the API mounts at /media.
    previous = media.use_storage(None)
    yield settings.media_dir
    media.use_storage(previous)


def _upload_product_image(owner, product_id, data=None, name="photo.png", content_type="image/png"):
    return owner.post(f"/api/owner/products/{product_id}/image", headers=HEADERS,
                      files={"file": (name, data if data is not None else png(), content_type)})


# ------------------------------------------------------------------ fake bucket


def test_upload_stores_webp_under_expected_key_and_delete_removes_it(owner_a, ids, fake_storage):
    res = _upload_product_image(owner_a, ids["charger"])
    assert res.status_code == 200, res.text
    [(key, (data, content_type))] = fake_storage.objects.items()
    assert KEY.match(key) and key.startswith("products/")
    assert content_type == "image/webp"
    img = Image.open(io.BytesIO(data))
    assert img.format == "WEBP" and max(img.size) == media.MAX_SIDE  # re-encoded and resized as before
    assert res.json()["image_url"] == f"/media/{key}"

    # A second upload replaces the first and deletes the old object.
    res = _upload_product_image(owner_a, ids["charger"], png(color=(10, 200, 10)))
    assert res.status_code == 200, res.text
    assert key not in fake_storage.objects
    [new_key] = fake_storage.objects
    assert KEY.match(new_key) and new_key != key

    delete_image(new_key)
    assert fake_storage.objects == {}


def test_shop_image_response_uses_public_base_url(owner_a, fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "media_public_base_url", "https://media.example.test")
    res = owner_a.post("/api/owner/shop/image", headers=HEADERS, files={"file": ("shop.png", png(), "image/png")})
    assert res.status_code == 200, res.text
    [key] = fake_storage.objects
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
    assert len(broken.objects) == 2  # the old object is orphaned, not an error


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
    assert CACHE_CONTROL == "public, max-age=31536000, immutable"


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


# ------------------------------------------------------------------ serializers


def test_media_url_is_absolute_when_a_base_url_is_set(monkeypatch, client, ids):
    from app.schemas.serializers import media_url

    assert media_url(None) is None and media_url("") is None
    assert media_url("products/abc.webp") == "/media/products/abc.webp"
    monkeypatch.setattr(settings, "media_public_base_url", "https://media.example.test")
    assert media_url("products/abc.webp") == "https://media.example.test/products/abc.webp"


def test_public_pages_use_the_base_url(monkeypatch, owner_a, client, ids, fake_storage):
    assert _upload_product_image(owner_a, ids["charger"]).status_code == 200
    [key] = fake_storage.objects
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
