"""The sitemap endpoint (P2.8): only what customers may see, no personal data, served from cache."""
import json

import pytest

from app.api.routes import catalog
from app.models.core import live_shop_filter
from tests.conftest import HEADERS, PASSWORD


@pytest.fixture(autouse=True)
def fresh_sitemap_cache():
    catalog.reset_sitemap_cache()
    yield
    catalog.reset_sitemap_cache()


@pytest.fixture
def hidden(db, ids):
    """For the length of one test: Shop B paused, and Shop A's charger listing deactivated."""
    db.shops.update_one({"_id": ids["shop_b"]}, {"$set": {"is_active": False}})
    db.products.update_one({"_id": ids["charger"]}, {"$set": {"is_active": False}})
    yield
    db.shops.update_one({"_id": ids["shop_b"]}, {"$set": {"is_active": True}})
    db.products.update_one({"_id": ids["charger"]}, {"$set": {"is_active": True}})


def _expected(db) -> tuple[set[str], set[int]]:
    """What customers may see right now, straight from the database (other test modules add shops too)."""
    live = {s.id: s.slug for s in db.shops.find(live_shop_filter())}  # active and verified (P3.3)
    listings = {p.id for p in db.products.find({"is_active": True}) if p.shop_id in live}
    return set(live.values()), listings


def test_sitemap_lists_only_active_listings_of_live_shops(client, db, ids, hidden):
    res = client.get("/api/sitemap")
    assert res.status_code == 200
    data = res.json()
    slugs = [s["slug"] for s in data["shops"]]
    listing_ids = [p["id"] for p in data["listings"]]
    assert "shop-a" in slugs and "shop-b" not in slugs  # Shop B is paused
    assert ids["tape"] in listing_ids
    assert ids["charger"] not in listing_ids  # inactive listing
    assert ids["tape_far"] not in listing_ids  # active listing, but its shop is not live
    assert (set(slugs), set(listing_ids)) == _expected(db)
    assert len(slugs) == len(set(slugs)) and len(listing_ids) == len(set(listing_ids))
    for entry in data["shops"] + data["listings"]:
        assert entry["updated_at"].endswith("Z")


def test_pages_the_sitemap_leaves_out_answer_404(client, ids, hidden):
    """Search engines see only what customers may see: a paused shop's page and its listings are 404 too."""
    assert client.get("/api/shops/shop-b").status_code == 404
    assert client.get(f"/api/products/{ids['tape_far']}").status_code == 404  # active listing, paused shop
    assert client.get(f"/api/products/{ids['charger']}").status_code == 404  # inactive listing
    assert client.get(f"/api/products/{ids['tape']}").status_code == 200


def test_sitemap_lists_every_live_shop_and_active_listing(client, db, ids):
    data = client.get("/api/sitemap").json()
    slugs, listing_ids = {s["slug"] for s in data["shops"]}, {p["id"] for p in data["listings"]}
    assert {"shop-a", "shop-b"} <= slugs
    assert {ids["charger"], ids["tape"], ids["tape_far"]} <= listing_ids
    assert (slugs, listing_ids) == _expected(db)


def test_sitemap_holds_no_personal_data(client, db):
    body = client.get("/api/sitemap").text
    data = json.loads(body)
    assert set(data) == {"generated_at", "shops", "listings"}
    assert all(set(s) == {"slug", "updated_at"} for s in data["shops"])
    assert all(set(p) == {"id", "updated_at"} for p in data["listings"])
    people = [(u.name, u.email, u.phone) for u in db.users.find()]
    shops = [(s.name, s.address_line, s.locality, s.phone, s.owner_id) for s in db.shops.find()]
    for value in {str(v) for row in people + shops for v in row if v}:
        if not value.isdigit():  # ids are numbers that can appear as listing ids
            assert value not in body, "personal or owner-entered data in the sitemap"
    assert PASSWORD not in body


def test_sitemap_is_served_from_cache_on_the_second_call(client, db, ids):
    first = client.get("/api/sitemap")
    assert first.headers["cache-control"] == "public, max-age=3600"
    db.products.update_one({"_id": ids["tape_far"]}, {"$set": {"is_active": False}})
    try:
        second = client.get("/api/sitemap")
        # The listing deactivated after the first call is still listed: the answer came from the cache.
        assert second.json() == first.json()
        assert ids["tape_far"] in [p["id"] for p in second.json()["listings"]]
        catalog.reset_sitemap_cache()
        third = client.get("/api/sitemap").json()
        assert ids["tape_far"] not in [p["id"] for p in third["listings"]]
    finally:
        db.products.update_one({"_id": ids["tape_far"]}, {"$set": {"is_active": True}})


def test_sitemap_cache_expires_after_an_hour(client, db, ids, monkeypatch):
    client.get("/api/sitemap")
    db.products.update_one({"_id": ids["tape_far"]}, {"$set": {"is_active": False}})
    try:
        now = catalog.time.monotonic()
        monkeypatch.setattr(catalog.time, "monotonic", lambda: now + catalog.SITEMAP_TTL_SECONDS + 1)
        data = client.get("/api/sitemap").json()
        assert ids["tape_far"] not in [p["id"] for p in data["listings"]]
    finally:
        db.products.update_one({"_id": ids["tape_far"]}, {"$set": {"is_active": True}})


def test_sitemap_is_a_public_catalogue_read(client):
    # No session needed, and it counts under the catalogue-read limit, like the other catalogue routes.
    from app.core import ratelimit

    row = ratelimit.match("GET", "/api/sitemap")
    assert row is not None and ratelimit.CATALOG_READ in row.rules
    assert client.get("/api/sitemap", headers=HEADERS).status_code == 200
