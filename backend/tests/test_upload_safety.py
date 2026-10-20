"""Upload and import hardening and photo thumbnails (P2.7, plan section 8.13).

Every test image and workbook is built here, in memory: the repository holds no binary fixtures. Photos go to
the in-memory fake storage from tests/test_media.py, so nothing reaches a bucket.
"""
import asyncio
import csv
import io
import struct
import threading
import time
import zipfile
import zlib

import httpx
import pytest
from PIL import Image, ImageFile, features
from pydantic import ValidationError

from app.api.routes import owner as owner_routes
from app.core.config import Settings, settings
from app.core.errors import AppError
from app.main import app
from app.services import importer, media
from app.services.media import THUMB_SIDE, thumbnail_key
from database import admin_tasks
from tests.conftest import HEADERS, PASSWORD
from tests.test_media import FakeStorage, photos, png

MB = 1024 * 1024


@pytest.fixture
def fake_storage():
    fake = FakeStorage()
    previous = media.use_storage(fake)
    yield fake
    media.use_storage(previous)


def blank_png(width: int, height: int) -> bytes:
    """A valid 1-bit black PNG of any canvas size, a few KB even at 10,000 by 10,000 px: built chunk by chunk,
    so no test ever holds the decoded pixels."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    row = b"\x00" * (1 + (width + 7) // 8)  # filter byte 0, then the packed bits
    deflate = zlib.compressobj(9)
    idat = b"".join(deflate.compress(row) for _ in range(height)) + deflate.flush()
    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)  # 1-bit greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def upload(client, product_id, data, name="photo.png", content_type="image/png"):
    return client.post(f"/api/owner/products/{product_id}/image", headers=HEADERS,
                       files={"file": (name, data, content_type)})


def preview(client, data: bytes, name: str):
    return client.post("/api/owner/import/preview", headers=HEADERS, files={"file": (name, data, "text/csv")})


def _fail_if_called(name: str, calls: list):
    def fail(*args, **kwargs):
        calls.append(name)
        raise AssertionError(f"{name} must not run")
    return fail


def _login(c, email="a@example.com"):
    res = c.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers=HEADERS)
    assert res.status_code == 200, res.text


# ------------------------------------------------------------------ request bodies over the limit


def test_body_over_the_limit_is_refused_on_an_upload_route(owner_a, ids, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(owner_routes, "save_image", _fail_if_called("save_image", calls))
    monkeypatch.setattr(owner_routes, "_own_product", _fail_if_called("handler", calls))
    body = b"x" * (6 * MB + 1)
    res = owner_a.post(f"/api/owner/products/{ids['charger']}/image", content=body,
                       headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=nearshop"})
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "body_too_large"
    assert res.headers["x-content-type-options"] == "nosniff"  # it still gets the API's headers
    assert calls == []


def test_body_over_the_limit_is_refused_on_a_json_route(owner_a, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(owner_routes.importer, "commit", _fail_if_called("commit", calls))
    body = b'{"rows": [' + b" " * (6 * MB + 1 - len(b'{"rows": []}')) + b"]}"
    assert len(body) == 6 * MB + 1
    res = owner_a.post("/api/owner/import/commit", content=body,
                       headers={**HEADERS, "Content-Type": "application/json"})
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "body_too_large"
    assert calls == []


def test_a_body_at_the_limit_is_not_refused(owner_a):
    # Exactly 6 MB reaches the route, whose own validation answers (an empty row list).
    body = b'{"rows": [' + b" " * (6 * MB - len(b'{"rows": []}')) + b"]}"
    assert len(body) == 6 * MB
    res = owner_a.post("/api/owner/import/commit", content=body,
                       headers={**HEADERS, "Content-Type": "application/json"})
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "validation_error"


def test_a_streamed_body_without_a_length_is_counted(ids, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(owner_routes.importer, "commit", _fail_if_called("commit", calls))

    async def chunks(total: int):
        sent = 0
        while sent < total:
            n = min(MB, total - sent)
            sent += n
            yield b" " * n

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            await c.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD}, headers=HEADERS)
            big = await c.post("/api/owner/import/commit", content=chunks(7 * MB),
                               headers={**HEADERS, "Content-Type": "application/json"})
            small = await c.post("/api/owner/import/commit", content=chunks(10),
                                 headers={**HEADERS, "Content-Type": "application/json"})
            return big, small

    big, small = asyncio.run(run())
    assert "content-length" not in big.request.headers  # sent chunked: only counting can catch it
    assert big.status_code == 413 and big.json()["error"]["code"] == "body_too_large"
    assert small.status_code == 422  # a small body still reaches the route (here: not valid JSON)
    assert calls == []


# ------------------------------------------------------------------ images


def test_a_huge_canvas_is_refused_from_its_header_without_decoding(owner_a, ids, fake_storage, monkeypatch):
    data = blank_png(10_000, 10_000)
    assert len(data) < 100 * 1024  # a small file...
    with Image.open(io.BytesIO(data)) as probe:
        assert probe.size == (10_000, 10_000)  # ...with a 100-megapixel canvas
    decoded: list[str] = []
    monkeypatch.setattr(ImageFile.ImageFile, "load", _fail_if_called("decoder", decoded))
    res = upload(owner_a, ids["charger"], data)
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "bad_image"
    assert decoded == [] and fake_storage.objects == {}


@pytest.mark.parametrize("size", [(6000, 5000), (8001, 100)])  # 30 megapixels; a side over 8,000 px
def test_pixel_and_side_limits(size, monkeypatch):
    decoded: list[str] = []
    monkeypatch.setattr(ImageFile.ImageFile, "load", _fail_if_called("decoder", decoded))
    with pytest.raises(AppError) as err:
        media.decode_image(blank_png(*size))
    assert err.value.status_code == 422 and err.value.code == "bad_image"
    assert decoded == []


def test_limits_are_settings(monkeypatch):
    monkeypatch.setattr(settings, "max_image_side", 50)
    with pytest.raises(AppError) as err:
        media.decode_image(png((60, 10)))
    assert err.value.status_code == 422
    monkeypatch.setattr(settings, "max_image_side", 8000)
    assert media.decode_image(png((60, 10))).size == (60, 10)


def test_an_image_above_the_bomb_limit_gets_422_not_500(owner_a, ids, fake_storage, monkeypatch):
    data = blank_png(15_000, 15_000)  # 225 megapixels: over twice Image.MAX_IMAGE_PIXELS
    with pytest.raises(Image.DecompressionBombError):
        Image.open(io.BytesIO(data))
    # With the header limits out of the way, only the catch of Pillow's own error stands between it and a 500.
    monkeypatch.setattr(settings, "max_image_pixels", 10**12)
    monkeypatch.setattr(settings, "max_image_side", 10**6)
    res = upload(owner_a, ids["charger"], data)
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "bad_image"
    assert fake_storage.objects == {}


def _first_frame_color(stored: bytes) -> tuple:
    with Image.open(io.BytesIO(stored)) as img:
        return img.convert("RGB").getpixel((5, 5))


def test_only_the_first_frame_of_an_animated_gif_is_used(owner_a, ids, fake_storage):
    frames = [Image.new("RGB", (40, 40), c) for c in ((255, 0, 0), (0, 0, 255), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    res = upload(owner_a, ids["charger"], buf.getvalue(), "anim.gif", "image/gif")
    assert res.status_code == 200, res.text
    [key] = photos(fake_storage)
    r, g, b = _first_frame_color(fake_storage.objects[key][0])
    assert r > 200 and g < 50 and b < 50


@pytest.mark.skipif(not features.check("webp"), reason="Pillow built without WebP")
def test_only_the_first_frame_of_an_animated_webp_is_used(owner_a, ids, fake_storage):
    frames = [Image.new("RGB", (40, 40), c) for c in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=100, lossless=True)
    with Image.open(io.BytesIO(buf.getvalue())) as probe:
        assert getattr(probe, "n_frames", 1) == 2
    res = upload(owner_a, ids["charger"], buf.getvalue(), "anim.webp", "image/webp")
    assert res.status_code == 200, res.text
    [key] = photos(fake_storage)
    r, g, b = _first_frame_color(fake_storage.objects[key][0])
    assert r > 200 and b < 50


# ------------------------------------------------------------------ workbooks and CSV


def _workbook(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _with_extra_entry(xlsx: bytes, name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(xlsx)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        dst.writestr(name, data)
    return out.getvalue()


def test_a_normal_workbook_still_previews(owner_a):
    res = preview(owner_a, _workbook([["Item name", "Quantity", "Selling price"],
                                      ["Astral PTFE Thread Seal Tape", 12, 40.5]]), "stock.xlsx")
    assert res.status_code == 200, res.text
    [row] = res.json()["rows"]
    assert row["quantity"] == 12 and row["price"] == 40.5


def test_a_zip_bomb_workbook_is_refused_before_parsing(owner_a, monkeypatch):
    import openpyxl

    bomb = _with_extra_entry(_workbook([["Item name"], ["Tape"]]), "xl/media/padding.bin", b"\0" * (20 * MB))
    assert len(bomb) < 1 * MB  # 20 MB of zeros packs to a few KB: a ratio near 1,000 to 1
    parsed: list[str] = []
    monkeypatch.setattr(openpyxl, "load_workbook", _fail_if_called("load_workbook", parsed))
    res = preview(owner_a, bomb, "stock.xlsx")
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "file_too_large"
    assert parsed == []


def test_a_workbook_that_unpacks_past_the_total_limit_is_refused(monkeypatch):
    import os

    monkeypatch.setattr(settings, "import_max_uncompressed_mb", 1)
    padded = _with_extra_entry(_workbook([["Item name"], ["Tape"]]), "xl/media/photo.bin", os.urandom(2 * MB))
    with pytest.raises(AppError) as err:
        importer.read_table(padded, "stock.xlsx")
    assert err.value.code == "file_too_large"


def test_a_file_that_is_not_a_workbook_is_a_bad_file():
    with pytest.raises(AppError) as err:
        importer.read_table(b"not a zip archive", "stock.xlsx")
    assert err.value.code == "bad_file"


def _csv(rows: list[list]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode()


def test_a_csv_over_the_row_limit_is_refused_without_reading_to_the_end(owner_a, monkeypatch):
    rows = [["Item name", "Quantity"], *([f"Item {i}", "1"] for i in range(2001))]
    # After row 2,001 comes a row 500 columns wide: reading that far would give too_many_columns instead.
    data = _csv(rows) + b"," * 500 + b"x\n" + _csv([["More", "1"]] * 5000)
    read: list[int] = []
    real_reader = csv.reader

    def counting_reader(*args, **kwargs):
        for row in real_reader(*args, **kwargs):
            read.append(1)
            yield row

    monkeypatch.setattr(importer.csv, "reader", counting_reader)
    res = preview(owner_a, data, "stock.csv")
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "too_many_rows"
    assert len(read) == 1 + 2001  # the header and the 2,001st data row, then it stopped


def test_the_row_limit_itself_is_accepted_and_blank_rows_do_not_count():
    rows = [["Item name", "Quantity"], *([f"Item {i}", "1"] if i % 2 else ["", ""] for i in range(4000))]
    df = importer.read_table(_csv(rows), "stock.csv")
    assert len(df) == 2000 and list(df.columns) == ["Item name", "Quantity"]


@pytest.mark.parametrize("name", ["stock.csv", "stock.xlsx"])
def test_a_file_wider_than_the_column_limit_is_refused(name):
    rows = [["Item name", "Quantity"], ["Tape", *range(100)]]  # 101 columns in the second row
    data = _csv(rows) if name.endswith(".csv") else _workbook(rows)
    with pytest.raises(AppError) as err:
        importer.read_table(data, name)
    assert err.value.code == "too_many_columns"


def test_csv_reading_keeps_the_header_rules():
    data = "﻿Item name,Qty,Qty,\n\nTape,4,5,extra\n".encode()
    df = importer.read_table(data, "stock.csv")
    assert list(df.columns) == ["Item name", "Qty", "Qty.1", "Unnamed: 3"]
    assert df.to_dict("records") == [{"Item name": "Tape", "Qty": "4", "Qty.1": "5", "Unnamed: 3": "extra"}]
    latin = importer.read_table("Item name\nCaf\xe9 tape\n".encode("latin-1"), "stock.csv")
    assert latin.iloc[0]["Item name"] == "Caf\xe9 tape"


def test_upload_limit_settings_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", max_request_body_mb=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", import_max_rows=0)


# ------------------------------------------------------------------ off the event loop


def test_a_slow_upload_never_stalls_other_requests(ids, fake_storage, monkeypatch):
    started = threading.Event()
    encoding: dict[str, float] = {}
    real_encode = media.encode_webp

    def slow_encode(img, side, quality):
        if not started.is_set():
            encoding["start"] = time.perf_counter()
            started.set()
            time.sleep(1.0)  # blocking, like a real encode of a large photo
            encoding["end"] = time.perf_counter()
        return real_encode(img, side, quality)

    monkeypatch.setattr(media, "encode_webp", slow_encode)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            res = await c.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD},
                               headers=HEADERS)
            assert res.status_code == 200, res.text
            task = asyncio.create_task(c.post(f"/api/owner/products/{ids['charger']}/image", headers=HEADERS,
                                              files={"file": ("photo.png", png((200, 100)), "image/png")}))
            deadline = time.monotonic() + 5
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert started.is_set(), "the upload never reached the encoder"
            t0 = time.perf_counter()
            health = await c.get("/api/health")
            done = time.perf_counter()
            uploaded = await task
            return health, t0, done, uploaded

    health, t0, done, uploaded = asyncio.run(run())
    assert health.status_code == 200
    # Answered while the encoder was still busy, and fast: a blocked event loop would only have noticed the
    # encoder had started once it had finished.
    assert done < encoding["end"], "the health request waited for the upload to finish"
    assert done - t0 < 0.1, f"/api/health took {(done - t0) * 1000:.0f} ms while an upload was encoding"
    assert uploaded.status_code == 200, uploaded.text


def test_the_upload_routes_are_plain_def_handlers():
    # FastAPI runs a def handler in its thread pool; an async def one would run its blocking work on the loop.
    for endpoint in (owner_routes.upload_shop_image, owner_routes.upload_product_image, owner_routes.import_preview):
        assert not asyncio.iscoroutinefunction(endpoint)


# ------------------------------------------------------------------ thumbnails


def test_a_thumbnail_is_stored_beside_the_photo_and_deleted_with_it(owner_a, client, ids, fake_storage):
    res = upload(owner_a, ids["charger"], png((1800, 900)))
    assert res.status_code == 200, res.text
    [key] = photos(fake_storage)
    thumb = thumbnail_key(key)
    assert thumb == key.removesuffix(".webp") + ".thumb.webp"
    data, content_type = fake_storage.objects[thumb]
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "WEBP" and max(img.size) <= THUMB_SIDE and img.size == (320, 160)
    assert content_type == "image/webp"
    assert res.json()["thumbnail_url"] == f"/media/{thumb}"
    product = client.get(f"/api/products/{ids['charger']}", headers=HEADERS).json()
    assert product["image_url"] == f"/media/{key}" and product["thumbnail_url"] == f"/media/{thumb}"

    # Replacing the photo deletes the old photo and its thumbnail.
    assert upload(owner_a, ids["charger"], png((300, 300))).status_code == 200
    assert key not in fake_storage.objects and thumb not in fake_storage.objects
    [new_key] = photos(fake_storage)
    assert set(fake_storage.objects) == {new_key, thumbnail_key(new_key)}
    media.delete_image(new_key)
    assert fake_storage.objects == {}


def test_shop_photo_thumbnail(owner_a, fake_storage):
    res = owner_a.post("/api/owner/shop/image", headers=HEADERS, files={"file": ("shop.png", png(), "image/png")})
    assert res.status_code == 200, res.text
    [key] = photos(fake_storage)
    assert res.json()["thumbnail_url"] == f"/media/{thumbnail_key(key)}"
    assert owner_a.get("/api/owner/shop", headers=HEADERS).json()["thumbnail_url"] == f"/media/{thumbnail_key(key)}"


def test_a_small_photo_is_never_enlarged(owner_a, ids, fake_storage):
    assert upload(owner_a, ids["tape"], png((120, 80))).status_code == 200
    [key] = photos(fake_storage)
    with Image.open(io.BytesIO(fake_storage.objects[thumbnail_key(key)][0])) as img:
        assert img.size == (120, 80)


def test_no_photo_is_left_without_its_thumbnail(owner_a, ids, fake_storage, monkeypatch):
    real_put = fake_storage.put

    def put(key, data, content_type):
        if key.endswith(".thumb.webp"):
            raise ConnectionError("bucket unreachable")
        real_put(key, data, content_type)

    monkeypatch.setattr(fake_storage, "put", put)
    with pytest.raises(ConnectionError):  # the test client re-raises the 500
        upload(owner_a, ids["tape"], png())
    assert fake_storage.objects == {}


def test_serializers_put_a_thumbnail_url_beside_every_image_url():
    from app.schemas import serializers

    assert serializers.thumbnail_url(None) is None and serializers.thumbnail_url("") is None
    assert serializers.thumbnail_url("shops/abc.webp") == "/media/shops/abc.thumb.webp"
    source = open(serializers.__file__).read()
    assert source.count('"image_url":') == source.count('"thumbnail_url":') == 4


# ------------------------------------------------------------------ backfill


@pytest.fixture
def legacy_photo(db, ids, fake_storage):
    """A product photo stored before thumbnails existed (no .thumb.webp), and one whose file is gone."""
    buf = io.BytesIO()
    Image.new("RGB", (1400, 700), (30, 120, 200)).save(buf, "WEBP", quality=82)
    key, gone = "products/" + "b" * 32 + ".webp", "shops/" + "c" * 32 + ".webp"
    fake_storage.put(key, buf.getvalue(), "image/webp")
    product, shop = db.products.get(ids["tape_far"]), db.shops.get(ids["shop_b"])
    before = product.image_path, shop.image_path
    db.products.update_one({"_id": product.id}, {"$set": {"image_path": key}})
    db.shops.update_one({"_id": shop.id}, {"$set": {"image_path": gone}})
    yield key
    db.products.update_one({"_id": product.id}, {"$set": {"image_path": before[0]}})
    db.shops.update_one({"_id": shop.id}, {"$set": {"image_path": before[1]}})


def test_backfill_creates_missing_thumbnails_and_is_idempotent(db, fake_storage, legacy_photo):
    puts: list[str] = []
    real_put = fake_storage.put
    fake_storage.put = lambda k, d, t: (puts.append(k), real_put(k, d, t))
    out: list[str] = []
    assert admin_tasks.backfill_thumbnails(db, ask=lambda _: db.mongo.name, out=out.append, batch_size=1,
                                           storage=fake_storage) == 0
    thumb = thumbnail_key(legacy_photo)
    assert puts == [thumb]
    with Image.open(io.BytesIO(fake_storage.objects[thumb][0])) as img:
        assert max(img.size) <= THUMB_SIDE and img.size == (320, 160)
    assert "Thumbnails: 1 created" in "\n".join(out)
    assert any("whose photo file is missing" in line for line in out)
    assert any(line.startswith("Median thumbnail size:") for line in out)

    snapshot = dict(fake_storage.objects)
    out.clear()
    assert admin_tasks.backfill_thumbnails(db, ask=lambda _: db.mongo.name, out=out.append,
                                           storage=fake_storage) == 0
    assert puts == [thumb] and fake_storage.objects == snapshot  # the second run creates nothing
    assert "Thumbnails: 0 created, 1 already there." in out


def test_backfill_needs_the_typed_confirmation(db, fake_storage, legacy_photo):
    out: list[str] = []
    assert admin_tasks.backfill_thumbnails(db, ask=lambda _: "wrong", out=out.append, storage=fake_storage) == 1
    assert thumbnail_key(legacy_photo) not in fake_storage.objects


def test_backfill_from_the_command_line(db, fake_storage, legacy_photo):
    out: list[str] = []
    code = admin_tasks.main(["backfill-thumbnails", "--batch-size", "2"], ask=lambda _: db.mongo.name, out=out.append)
    assert code == 0 and thumbnail_key(legacy_photo) in fake_storage.objects
    assert all("@" not in line for line in out)  # never an email address
