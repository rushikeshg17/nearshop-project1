"""Search interaction logging, online metrics and shadow ranking (P5.6; plan sections 8.4 and 8.6).

Background writes run inline here (and swallow errors, as db.background does), so each request's events are in
the database when it returns."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo.errors import WriteError

from app import main
from app.core import ratelimit
from app.core.config import RANKING_CONFIGS, RankingConfig, settings
from app.core.database import Database, utcnow
from app.models import CatalogItem
from app.services import search as search_module
from app.services import search_events as se
from app.services.search import SearchParams, run_search
from app.utils.text import normalize_query
from database import schema
from tests.conftest import CENTER, HEADERS, login

AT = {"lat": CENTER[0], "lng": CENTER[1]}
SEARCH = {"q": "teflon tape", **AT, "radius_km": 5}
# Every field a search event may hold (database/schema.py allows no other).
EVENT_FIELDS = {"_id", "created_at", "query", "redacted", "language", "category", "sort", "filters", "radius_km",
                "lat", "lng", "results_count", "corrected", "ranking", "impressions", "clicks", "conversions",
                "previous_search_id", "shadow"}
FORBIDDEN = ("user", "session", "ip", "agent", "phone", "email", "reservation", "order", "address")


@pytest.fixture(autouse=True)
def measured_world(ids, monkeypatch):
    """Inline background writes; no events, consents or snapshots before or after each test."""
    def inline(fn):
        try:
            fn()
        except Exception:  # as db.background: a failed write never reaches the request
            pass

    monkeypatch.setattr(Database, "background", staticmethod(inline))
    db = Database()

    def clean():
        db.mongo[se.COLLECTION].delete_many({})
        db.mongo["consents"].delete_many({})
        db.model_runs.delete_many({"name": se.SNAPSHOT_NAME})
        se.reset_terms()

    clean()
    yield
    clean()


def consent(email: str, granted: bool = True) -> None:
    """Append a consents row for search_measurement (P3.1's append-only rows: the newest one is in force)."""
    db = Database()
    user = db.users.find_one({"email": email})
    db.mongo["consents"].insert_one({"_id": db.next_id("consents"), "user_id": user.id,
                                     "purpose": se.CONSENT_PURPOSE, "notice_version": "v1", "granted": granted,
                                     "at": utcnow(), "source": "web"})


def events() -> list[dict]:
    return list(Database().mongo[se.COLLECTION].find().sort("created_at", 1))


def trail_cookie(res) -> str | None:
    """The Set-Cookie header for the trail, if the response sent one."""
    found = [h for h in res.headers.get_list("set-cookie") if h.startswith(f"{se.TRAIL_COOKIE}=")]
    return found[0] if found else None


@pytest.fixture
def consented(customer):
    consent("cust@example.com")
    return customer


def _keys(value, prefix=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{prefix}{k}"
            yield from _keys(v, f"{prefix}{k}.")
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v, prefix)


# ---------------------------------------------------------------- who is logged


def test_no_event_without_consent_signed_out_for_owners_and_admins_or_with_log_false(client, ids):
    res = client.get("/api/search", params=SEARCH)
    assert res.status_code == 200 and "search_id" not in res.json() and trail_cookie(res) is None

    login(client, "cust@example.com")  # a customer without the consent
    res = client.get("/api/search", params=SEARCH)
    assert "search_id" not in res.json() and trail_cookie(res) is None
    consent("cust@example.com", granted=False)
    assert trail_cookie(client.get("/api/search", params=SEARCH)) is None

    for email in ("a@example.com", "admin@example.com"):  # consent rows do not make owners or admins measured
        consent(email)
        with TestClient(main.app) as c:
            login(c, email)
            res = c.get("/api/search", params=SEARCH)
            assert res.status_code == 200 and "search_id" not in res.json() and trail_cookie(res) is None

    db = Database()
    run_search(db, SearchParams(q="teflon tape", lat=CENTER[0], lng=CENTER[1]), log=False)
    assert client.get("/api/ai/search-compare", params={"q": "teflon tape"}).status_code == 200
    assert events() == []


def test_a_consented_search_writes_one_event_with_nothing_that_identifies_anyone(consented, ids):
    res = consented.get("/api/search", params={**SEARCH, "lat": 12.30523, "lng": 76.65527})
    body = res.json()
    [event] = events()
    assert body["search_id"] == event["_id"] and len(event["_id"]) == 24
    assert set(event) <= EVENT_FIELDS
    assert not [k for k in _keys(event) if any(word in k.lower() for word in FORBIDDEN)]
    assert (event["lat"], event["lng"]) == (12.31, 76.66)  # 2 decimals
    assert event["query"] == "teflon tape" and event["redacted"] is False and event["language"] == "en"
    assert event["results_count"] == body["total"] and event["ranking"] == settings.ranking.version
    assert event["impressions"][0] == {"group": body["groups"][0]["key"],
                                       "listing": body["groups"][0]["best_offer"]["id"], "position": 1}
    assert event["clicks"] == [] and event["conversions"] == [] and event["shadow"] is None
    cookie = trail_cookie(res)
    assert cookie and "HttpOnly" in cookie and "Path=/api" in cookie and "samesite=lax" in cookie.lower()
    assert "Max-Age=7200" in cookie

    consented.get("/api/search", params={**SEARCH, "q": "charger 9876543210", "category": "call 9876543210"})
    consented.get("/api/search", params={**SEARCH, "q": "ravi@gmail.com tape"})
    consented.get("/api/search", params={**SEARCH, "q": "tape " + "x" * 60})
    for redacted in events()[1:]:
        assert redacted["query"] == "" and redacted["redacted"] is True
    assert events()[1]["category"] is None  # only a real category slug is kept
    assert events()[1]["previous_search_id"] == event["_id"]  # the trail's previous search, under 2 minutes old


def test_the_database_refuses_an_event_with_an_identifying_field(consented):
    consented.get("/api/search", params=SEARCH)
    [event] = events()
    db = Database()
    with pytest.raises(WriteError):
        db.mongo[se.COLLECTION].insert_one({**event, "_id": "f" * 24, "user_id": 1})
    with pytest.raises(WriteError):
        db.mongo[se.COLLECTION].update_one({"_id": event["_id"]}, {"$set": {"filters.ip": "203.0.113.1"}})


def test_an_auto_corrected_search_is_one_event_with_the_corrected_flag(consented, monkeypatch):
    # Keyword matches only: none for tefloon. Search scores only the listings in radius (P8.2), through
    # nearby_similarities rather than index.query.
    monkeypatch.setattr(search_module, "nearby_similarities", lambda db, text: lambda ids: {})
    body = consented.get("/api/search", params={**SEARCH, "q": "tefloon"}).json()
    assert body["corrected_from"] == "tefloon"
    [event] = events()
    assert event["corrected"] is True and event["query"] == "tefloon" and event["results_count"] == body["total"]


# ---------------------------------------------------------------- language tags


@pytest.fixture
def umbrella(db, ids):
    """A catalogue item with Kannada, Telugu and Hindi names and their Latin spellings (P5.2). Removed after the
    test; no search runs while it exists, so no cached vocabulary sees it."""
    category = db.categories.find_one({})
    item = db.catalog_items.insert(CatalogItem(
        slug="test-umbrella", name="Test Umbrella", category_id=category.id, subcategory="umbrellas",
        typical_price=200, icon="Umbrella"))
    # local_terms is written by migration 4 and the seed, not through the model.
    db.catalog_items.raw.update_one({"_id": item.id}, {"$set": {
        "local_terms": ["ಕೊಡೆ", "ಛತ್ರಿ", "గొడుగు", "छाता", "kode", "godugu", "chhata"]}})
    se.reset_terms()
    yield item
    db.catalog_items.raw.delete_one({"_id": item.id})
    se.reset_terms()


def test_language_tags(db, umbrella):
    tag = lambda q: se.language_tag(db, normalize_query(q))  # noqa: E731
    assert tag("ಚಾರ್ಜರ್") == "kn"
    assert tag("మొబైల్ ఛార్జర్") == "te"
    assert tag("मोबाइल चार्जर") == "hi"
    assert tag("charger ಚಾರ್ಜರ್ चार्जर") == "kn" and tag("चार्जर ಚಾರ್ಜರ್") == "hi"  # the first non-Latin script
    assert tag("kode") == "kn-Latn" and tag("blue kode") == "kn-Latn"  # Kannada in Latin script, a local term
    assert tag("godugu") == "te-Latn" and tag("chhata") == "hi-Latn"
    assert tag("umbrella") == "en" and tag("phone charger") == "en"
    assert se.latin_terms(db) == {"kode": "kn-Latn", "godugu": "te-Latn", "chhata": "hi-Latn"}


# ---------------------------------------------------------------- search id and trail


def test_a_later_page_reuses_the_search_id_and_adds_its_impressions(consented, ids):
    params = {"q": "samsung astral tape charger", **AT, "radius_km": 50, "page_size": 1}
    first = consented.get("/api/search", params=params).json()
    assert first["total"] >= 2, first
    second = consented.get("/api/search", params={**params, "page": 2}).json()
    assert second["search_id"] == first["search_id"]
    [event] = events()
    assert [i["position"] for i in event["impressions"]] == [1, 2]
    assert event["impressions"][1]["group"] == second["groups"][0]["key"]
    other = consented.get("/api/search", params={**params, "page": 2, "radius_km": 40}).json()
    assert "search_id" not in other and len(events()) == 1  # other filters: not a later page of that search
    third = consented.get("/api/search", params=params).json()  # page 1 again: a new search
    assert third["search_id"] != first["search_id"] and len(events()) == 2


def test_the_trail_cookie_only_with_the_consent_and_withdrawal_deletes_it(consented, ids):
    res = consented.get("/api/search", params=SEARCH)
    assert trail_cookie(res)
    cookie = consented.cookies.get(se.TRAIL_COOKIE)
    group, listing = res.json()["groups"][0]["key"], res.json()["groups"][0]["best_offer"]["id"]

    consent("cust@example.com", granted=False)  # withdrawn: the next search deletes the cookie
    res = consented.get("/api/search", params=SEARCH)
    deleted = trail_cookie(res)
    assert deleted and 'Max-Age=0' in deleted and "search_id" not in res.json()
    assert consented.cookies.get(se.TRAIL_COOKIE) is None

    consented.cookies.set(se.TRAIL_COOKIE, cookie)  # a click and a reservation sent with the old cookie
    click = consented.post("/api/search/click", json={"listing_id": listing, "group": group, "position": 1},
                           headers=HEADERS)
    assert click.status_code == 204
    consented.cookies.set(se.TRAIL_COOKIE, cookie)
    r = consented.post("/api/reservations", json={"product_id": listing, "quantity": 1}, headers=HEADERS)
    assert r.status_code == 200
    consented.post(f"/api/reservations/{r.json()['id']}/cancel", json={}, headers=HEADERS)
    [event] = events()
    assert event["clicks"] == [] and event["conversions"] == []


def test_a_signed_out_visitor_with_a_trail_cookie_gets_it_deleted(consented, client, ids):
    consented.get("/api/search", params=SEARCH)
    cookie = consented.cookies.get(se.TRAIL_COOKIE)
    with TestClient(main.app) as anon:
        anon.cookies.set(se.TRAIL_COOKIE, cookie)
        res = anon.get("/api/search", params=SEARCH)
        assert "Max-Age=0" in (trail_cookie(res) or "")
    assert len(events()) == 1


def test_a_tampered_trail_cookie_is_ignored(consented, ids):
    body = consented.get("/api/search", params=SEARCH).json()
    cookie = consented.cookies.get(se.TRAIL_COOKIE)
    payload, _, signature = cookie.rpartition(".")
    consented.cookies.set(se.TRAIL_COOKIE, payload + "." + ("0" if signature[0] != "0" else "1") + signature[1:])
    group = body["groups"][0]
    consented.post("/api/search/click", json={"listing_id": group["best_offer"]["id"], "group": group["key"],
                                              "position": 1}, headers=HEADERS)
    assert events()[0]["clicks"] == []


# ---------------------------------------------------------------- clicks and attribution


def _search_and_click(c, params=SEARCH, index=0, listing=None):
    body = c.get("/api/search", params=params).json()
    group = body["groups"][index]
    listing = listing or group["best_offer"]["id"]
    res = c.post("/api/search/click", json={"listing_id": listing, "group": group["key"], "position": index + 1},
                 headers=HEADERS)
    assert res.status_code == 204 and res.content == b""
    return body, group, listing


def _reserve(c, product_id):
    r = c.post("/api/reservations", json={"product_id": product_id, "quantity": 1}, headers=HEADERS)
    assert r.status_code == 200, r.text
    c.post(f"/api/reservations/{r.json()['id']}/cancel", json={}, headers=HEADERS)  # at most 1 active
    return r.json()


def test_a_click_goes_to_the_right_search_and_a_reservation_after_it_converts(consented, ids):
    body, group, listing = _search_and_click(consented)
    consented.get("/api/search", params={**SEARCH, "q": "charger"})  # a later search without that group
    first, later = events()
    assert first["_id"] == body["search_id"]
    assert [(c["group"], c["listing"], c["position"]) for c in first["clicks"]] == [(group["key"], listing, 1)]
    assert later["clicks"] == []

    _reserve(consented, listing)
    [conversion] = events()[0]["conversions"]
    assert (conversion["kind"], conversion["group"], conversion["position"]) == ("reservation", group["key"], 1)
    assert events()[1]["conversions"] == []


def test_another_listing_of_the_same_item_and_an_order_convert(consented, ids):
    wide = {**SEARCH, "radius_km": 50}
    _, group, _ = _search_and_click(consented, wide, listing=ids["tape"])
    assert group["key"].startswith("c")
    _reserve(consented, ids["tape_far"])  # the same catalogue item at another shop
    assert [c["kind"] for c in events()[0]["conversions"]] == ["reservation"]

    _search_and_click(consented, {**SEARCH, "q": "charger"})
    order = consented.post("/api/orders", json={
        "items": [{"product_id": ids["tape"], "quantity": 1}, {"product_id": ids["charger"], "quantity": 1}],
        **AT, "address": "12 Temple Street, Agrahara", "phone": "9876543210"}, headers=HEADERS)
    assert order.status_code == 200, order.text
    consented.post(f"/api/orders/{order.json()['id']}/cancel", json={}, headers=HEADERS)
    # The latest click wins: the charger search gets the order, the tape search keeps its one reservation.
    tape, charger = events()
    assert [c["kind"] for c in tape["conversions"]] == ["reservation"]
    assert [c["kind"] for c in charger["conversions"]] == ["order"]


def test_no_conversion_without_a_click_or_after_2_hours(consented, ids, monkeypatch):
    consented.get("/api/search", params=SEARCH)
    _reserve(consented, ids["tape"])  # no click
    assert events()[0]["conversions"] == []

    _, _, listing = _search_and_click(consented)
    later = utcnow() + timedelta(hours=2, minutes=1)
    monkeypatch.setattr(se, "now", lambda: later)
    _reserve(consented, listing)
    assert all(e["conversions"] == [] for e in events())


def test_an_error_in_attribution_does_not_fail_the_reservation(consented, ids, monkeypatch):
    _, _, listing = _search_and_click(consented)

    def broken(*args, **kwargs):
        raise RuntimeError("attribution broke")

    monkeypatch.setattr(se, "_attribute", broken)
    _reserve(consented, listing)
    monkeypatch.setattr(se.Trail, "decode", broken)
    _reserve(consented, listing)
    assert events()[0]["conversions"] == []


def test_the_click_endpoint_answers_204_and_needs_the_client_header(client, consented, ids):
    payload = {"listing_id": ids["tape"], "group": "c1", "position": 3}
    with TestClient(main.app) as anon:
        assert anon.post("/api/search/click", json=payload, headers=HEADERS).status_code == 204  # no trail
        assert anon.post("/api/search/click", json=payload).status_code == 403
    _search_and_click(consented)  # with a trail
    assert consented.post("/api/search/click", json=payload, headers=HEADERS).status_code == 204  # not shown
    assert len(events()[0]["clicks"]) == 1
    assert ratelimit.match("POST", "/api/search/click").rules == (ratelimit.SEARCH_CLICK_IP,)


@pytest.fixture
def enforced(monkeypatch):
    clock = {"t": (utcnow() + timedelta(days=30)).replace(second=1).timestamp()}
    monkeypatch.setattr(ratelimit, "now", lambda: clock["t"])
    monkeypatch.setattr(settings, "rate_limit_mode", "enforce")
    monkeypatch.setattr(settings, "edge_secret", SecretStr("edge-secret-for-click-tests-" + "y" * 16))
    monkeypatch.setattr(settings, "edge_secret_required", False)
    Database().mongo[ratelimit.COLLECTION].delete_many({})
    yield
    Database().mongo[ratelimit.COLLECTION].delete_many({})


def test_the_121st_click_from_one_ip_in_a_minute_is_refused(enforced, ids):
    headers = {**HEADERS, "X-NearShop-Edge": settings.edge_secret.get_secret_value(),
               "X-NearShop-Client-IP": "198.51.100.77"}
    payload = {"listing_id": ids["tape"], "group": "c1", "position": 1}
    with TestClient(main.app) as c:
        for n in range(120):
            assert c.post("/api/search/click", json=payload, headers=headers).status_code == 204, n
        res = c.post("/api/search/click", json=payload, headers=headers)
        assert res.status_code == 429 and res.json()["error"]["code"] == "rate_limited"


# ---------------------------------------------------------------- metrics


def _event(at, n, language="en", query="charger", results=3, clicks=(), conversions=(), previous=None, **extra):
    return {"_id": f"{n:024x}", "created_at": at, "query": query, "redacted": False, "language": language,
            "category": None, "sort": "relevance",
            "filters": {"min_price": None, "max_price": None, "in_stock_only": False, "fulfillment": None},
            "radius_km": 5.0, "lat": 12.31, "lng": 76.66, "results_count": results, "corrected": False,
            "ranking": "r1", "impressions": [{"group": "c1", "listing": 1, "position": 1}],
            "clicks": [{"group": "c1", "listing": 1, "position": pos, "at": at + timedelta(seconds=s)}
                       for pos, s in clicks],
            "conversions": [{"kind": "reservation", "group": "c1", "position": 1, "at": at + timedelta(minutes=m)}
                            for m in conversions],
            "previous_search_id": previous, "shadow": None, **extra}


def _row(report, days, language):
    window = next(w for w in report["windows"] if w["days"] == days)
    return next((r for r in window["rows"] if r["language"] == language), None)


@pytest.fixture
def future():
    """A clock a year ahead, so hand-built rows are alone in the windows; search_history rows removed after."""
    at = utcnow() + timedelta(days=400)
    written: list[int] = []
    yield at, written
    Database().search_history.delete_many({"_id": {"$in": written}})


def test_each_metric_definition_on_hand_built_events(db, future):
    at, _ = future
    t = at - timedelta(days=1)
    m = lambda s: timedelta(seconds=s)  # noqa: E731
    rows = [
        _event(t, 1, clicks=[(2, 30)]),  # clicked at position 2: success
        _event(t + m(1000), 2, query="chager", results=0),  # reformulated by 3 within 2 minutes, no click between
        _event(t + m(1060), 3, previous=f"{2:024x}", clicks=[(1, 5), (4, 9)], conversions=[30]),
        _event(t + m(2000), 4, query="tap"),  # followed after 3 minutes: not a reformulation
        _event(t + m(2180), 5, query="tape", previous=f"{4:024x}"),
        _event(t + m(3000), 6, query="bulb", clicks=[(5, 10)]),  # a click before the next search
        _event(t + m(3060), 7, query="led bulb", previous=f"{6:024x}"),
        _event(t + m(4000), 8, query="fan"),  # the same query again: not a reformulation
        _event(t + m(4030), 9, query="fan", previous=f"{8:024x}", conversions=[180]),  # converted after 3 hours
        _event(at - timedelta(days=20), 10, results=0),  # only in the 30-day window
    ]
    db.mongo[se.COLLECTION].insert_many(rows)
    report = se.online_metrics(db, at)
    week, month = _row(report, 7, "all"), _row(report, 30, "all")
    assert week["searches"] == 9 and month["searches"] == 10
    assert week["success_rate"] == {"value": round(3 / 9, 4), "n": 9}  # events 1, 3 and 6
    assert week["zero_result_rate"] == {"value": round(1 / 9, 4), "n": 9}
    assert month["zero_result_rate"] == {"value": 0.2, "n": 10}
    assert week["reformulation_rate"] == {"value": round(1 / 9, 4), "n": 9}  # event 2 only
    assert week["click_position"] == {"mean": round((2 + 1 + 5) / 3, 2), "median": 2, "top3_share": round(2 / 3, 4),
                                      "n": 3}
    assert week["conversion_rate"] == {"value": round(2 / 9, 4), "n": 9}  # events 3 and 9
    en = _row(report, 7, "en")
    assert not en["enough_data"] and en["searches"] == 9
    assert en["success_rate"] == {"value": None, "n": 9}  # under 100 searches: not enough data
    assert en["click_position"]["mean"] is None


def test_the_100_search_threshold_and_the_history_zero_result_rate(db, future, umbrella):
    at, written = future
    t = at - timedelta(days=2)
    db.mongo[se.COLLECTION].insert_many(
        [_event(t, n, results=0 if n < 10 else 2) for n in range(100)]
        + [_event(t, 1000 + n, language="kn", query="ಕೊಡೆ") for n in range(99)])
    history = [{"_id": db.next_id("search_history"), "query": q, "normalized_query": q, "results_count": r,
                "created_at": t, "location": None, "user_id": None}
               for q, r, count in (("charger", 0, 30), ("charger", 4, 70), ("kode", 0, 25), ("kode", 1, 75),
                                   ("ಕೊಡೆ", 0, 5)) for _ in range(count)]
    db.search_history.raw.insert_many(history)
    written.extend(h["_id"] for h in history)
    report = se.online_metrics(db, at)
    en, kn = _row(report, 7, "en"), _row(report, 7, "kn")
    assert en["enough_data"] and en["searches"] == 100 and en["zero_result_rate"] == {"value": 0.1, "n": 100}
    assert not kn["enough_data"] and kn["searches"] == 99 and kn["success_rate"]["value"] is None
    # search_history over every search, tagged when read: kode is Kannada in Latin script.
    assert en["zero_result_rate_all"] == {"value": 0.3, "n": 100}
    kn_latn = _row(report, 7, "kn-Latn")
    assert kn_latn["searches"] == 0 and kn_latn["zero_result_rate_all"] == {"value": 0.25, "n": 100}
    assert kn["history_searches"] == 5 and kn["zero_result_rate_all"]["value"] is None  # under 100
    overall = _row(report, 7, "all")  # every language, with no threshold
    assert overall["zero_result_rate_all"] == {"value": round(60 / 205, 4), "n": 205}
    assert overall["searches"] == 199 and overall["zero_result_rate"] == {"value": round(10 / 199, 4), "n": 199}


# ---------------------------------------------------------------- shadow mode


def test_a_shadow_candidate_equal_to_production_gives_overlap_1(consented, monkeypatch):
    monkeypatch.setattr(settings, "search_shadow_ranking_version", settings.ranking.version)
    monkeypatch.setattr(settings, "search_shadow_share", 1.0)
    for q in ("teflon tape", "charger", "samsung astral tape charger", "nothing like this zzqx"):
        consented.get("/api/search", params={**SEARCH, "q": q, "radius_km": 50})
    shadowed = events()
    assert len(shadowed) == 4 and all(e["shadow"] for e in shadowed)
    for e in shadowed:
        assert e["shadow"]["top"] == e["shadow"]["production_top"]
        assert e["shadow"]["top"][:len(e["impressions"])] == [i["group"] for i in e["impressions"]][:20]
    [row] = se.shadow_comparison(Database())
    assert row["searches"] == 4 and row["mean_overlap_top10"] == 1.0
    assert row["zero_result"]["difference"] == 0 and row["zero_result"]["difference_pp"] == 0
    assert row["label"] == f"r1 + mongodb-text-index + {shadowed[0]['shadow']['vector']}"
    assert row["engine_candidate"] is False and row["guardrails"][3]["pass"] is None
    assert row["guardrails"][0]["pass"] is False  # 4 searches in less than a day


def test_a_different_candidate_never_changes_the_response(consented, monkeypatch):
    params = {**SEARCH, "q": "samsung astral tape charger", "radius_km": 50}
    plain = consented.get("/api/search", params=params).json()
    candidate = RankingConfig(version="r-test", w_relevance=0.05, w_proximity=0.9, w_in_stock=0.05,
                              relative_cutoff=0.01)
    monkeypatch.setitem(RANKING_CONFIGS, "r-test", candidate)
    monkeypatch.setattr(settings, "search_shadow_ranking_version", "r-test")
    monkeypatch.setattr(settings, "search_shadow_share", 1.0)
    shadowed = consented.get("/api/search", params=params).json()
    strip = lambda b: {k: v for k, v in b.items() if k != "search_id"}  # noqa: E731
    assert strip(shadowed) == strip(plain)
    event = events()[-1]
    assert event["shadow"]["ranking"] == "r-test" and event["ranking"] == "r1"
    assert event["shadow"]["production_top"] == [g["key"] for g in plain["groups"]]


def test_the_shadow_share_is_respected(consented, monkeypatch):
    monkeypatch.setattr(settings, "search_shadow_ranking_version", "r1")
    monkeypatch.setattr(settings, "search_shadow_share", 0.5)
    for draw in (0.6, 0.4, 0.5, 0.0):
        monkeypatch.setattr(se, "sample", lambda d=draw: d)
        consented.get("/api/search", params=SEARCH)
    assert [e["shadow"] is not None for e in events()] == [False, True, False, True]
    monkeypatch.setattr(settings, "search_shadow_share", 0.0)
    monkeypatch.setattr(se, "sample", lambda: 0.0)
    consented.get("/api/search", params=SEARCH)
    assert events()[-1]["shadow"] is None


def test_shadow_guardrails_on_hand_built_shadow_events(db):
    start = utcnow() - timedelta(days=8)
    shadow = {"label": "r2 + mongodb-text-index + x", "ranking": "r2", "keyword": "mongodb-text-index", "vector": "x",
              "production_label": "r1 + mongodb-text-index + x", "top": ["c2", "c1"], "production_top": ["c1", "c2"],
              "results_count": 2, "production_results_count": 2, "ms": 5.0, "production_ms": 4.0}
    rows = [_event(start + timedelta(minutes=40 * n), 5000 + n, clicks=[(1, 5)], shadow=shadow) for n in range(300)]
    db.mongo[se.COLLECTION].insert_many(rows)
    [row] = se.shadow_comparison(db)
    assert row["searches"] == 300 and row["days_covered"] >= 7 and row["mean_overlap_top10"] == 1.0
    assert row["clicked"] == {"searches": 300, "in_candidate_top10": 300, "in_candidate_top10_share": 1.0,
                              "mean_rank_change": 1.0}
    assert [g["pass"] for g in row["guardrails"]] == [True, True, True, None] and row["passed"]


# ---------------------------------------------------------------- admin view, snapshot, TTL


def test_the_online_metrics_are_admin_only_and_a_snapshot_holds_no_query_text(client, consented, ids):
    consented.get("/api/search", params=SEARCH)
    assert consented.get("/api/search/online").status_code == 403
    with TestClient(main.app) as anon:
        assert anon.get("/api/search/online").status_code == 401
        assert anon.post("/api/search/online/snapshot", headers=HEADERS).status_code == 401
    with TestClient(main.app) as admin:
        login(admin, "admin@example.com")
        report = admin.get("/api/search/online").json()
        assert [w["days"] for w in report["windows"]] == [7, 30] and report["ranking"] == settings.ranking.version
        assert _row(report, 30, "all")["searches"] == 1
        saved = admin.post("/api/search/online/snapshot", headers=HEADERS).json()
        assert saved["ranking"] == settings.ranking.version
        assert admin.get("/api/search/online").json()["snapshots"][0]["id"] == saved["id"]
    run = Database().model_runs.get(saved["id"])
    assert run.name == "search_online" and run.metrics["ranking"] == "r1" and run.metrics["window_days"] == [7, 30]
    assert "teflon" not in json.dumps(run.metrics, default=str)


def test_the_ttl_index_expires_events_after_90_days(db):
    info = db.mongo[se.COLLECTION].index_information()["created_ttl_90d"]
    assert info["key"] == [("created_at", 1)] and info["expireAfterSeconds"] == 90 * 24 * 60 * 60
    assert schema.SEARCH_EVENTS_KEEP_SECONDS == 90 * 86400


# ---------------------------------------------------------------- review fixes


def test_the_event_is_inserted_before_its_language_is_tagged(consented, monkeypatch):
    """The first language tag after start-up reads the catalogue's local terms; a later page or a click that comes
    meanwhile must already find the event."""
    seen: list[int] = []
    real = se.language_tag

    def slow_tag(db, norm, terms=None):
        seen.append(Database().mongo[se.COLLECTION].count_documents({}))
        return real(db, norm, terms)

    monkeypatch.setattr(se, "language_tag", slow_tag)
    consented.get("/api/search", params={**SEARCH, "q": "ಕೊಡೆ"})
    assert seen == [1]
    assert events()[0]["language"] == "kn"  # tagged after the insert


def test_a_later_page_waits_for_its_search_to_be_inserted(db, monkeypatch):
    """_add_impressions retries, off the request path, while the page-1 insert is still queued."""
    doc = _event(utcnow(), 7000)
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2 and db.mongo[se.COLLECTION].count_documents({"_id": doc["_id"]}) == 0:
            db.mongo[se.COLLECTION].insert_one(doc)  # the page-1 insert lands while the later page waits

    monkeypatch.setattr(se.time, "sleep", sleep)
    se._add_impressions(db.mongo.name, doc["_id"], [{"group": "c9", "listing": 9, "position": 2}])
    assert len(sleeps) == 2
    assert [i["position"] for i in db.mongo[se.COLLECTION].find_one({"_id": doc["_id"]})["impressions"]] == [1, 2]
    sleeps.clear()
    se._add_impressions(db.mongo.name, "f" * 24, [{"group": "c9", "listing": 9, "position": 2}])  # never inserted
    assert len(sleeps) == se.IMPRESSION_RETRIES - 1


def test_seven_calendar_days_of_shadow_pass_the_days_guardrail(db):
    """A run from day 1 at 00:05 to day 7 at 23:50 (Ballari time) covers 7 days, though under 7 x 24 hours."""
    day1 = datetime(2026, 11, 2, 0, 5, tzinfo=se.IST).astimezone(UTC).replace(tzinfo=None)
    day7 = datetime(2026, 11, 8, 23, 50, tzinfo=se.IST).astimezone(UTC).replace(tzinfo=None)
    shadow = {"label": "r2 + mongodb-text-index + x", "ranking": "r2", "keyword": "mongodb-text-index", "vector": "x",
              "production_label": "r1 + mongodb-text-index + x", "top": ["c1"], "production_top": ["c1"],
              "results_count": 1, "production_results_count": 1, "ms": 5.0, "production_ms": 4.0}
    step = (day7 - day1) / 299
    rows = [_event(day1 + step * n, 8000 + n, clicks=[(1, 5)], shadow=shadow) for n in range(300)]
    db.mongo[se.COLLECTION].insert_many(rows)
    [row] = se.shadow_comparison(db)
    assert row["elapsed_days"] < 7 and row["days_covered"] == 7 and row["guardrails"][0]["pass"] is True
    assert se.calendar_days(day1, day1 + timedelta(days=5, hours=23)) == 6


def test_a_search_served_while_the_vector_index_loads_is_not_shadowed(consented, monkeypatch):
    """Its label would read "loading" and split the candidate into a row of its own."""
    monkeypatch.setattr(settings, "search_shadow_ranking_version", settings.ranking.version)
    monkeypatch.setattr(settings, "search_shadow_share", 1.0)
    real = search_module.run_search

    def loading(*args, **kwargs):
        out = real(*args, **kwargs)
        return {**out, "engine": {**out["engine"], "semantic": se.VECTOR_LOADING}}

    monkeypatch.setattr(search_module, "run_search", loading)
    consented.get("/api/search", params=SEARCH)
    monkeypatch.setattr(search_module, "run_search", real)
    consented.get("/api/search", params=SEARCH)
    first, second = events()
    assert first["shadow"] is None and second["shadow"] is not None
    assert [r["label"] for r in se.shadow_comparison(Database())] == [second["shadow"]["label"]]
    assert "loading" not in second["shadow"]["label"]


def test_the_click_endpoint_answers_204_to_any_body(consented, ids):
    """Past the CSRF guard and the rate limit, a body that is not a click records nothing and still gets 204."""
    _search_and_click(consented)
    before = events()[0]["clicks"]
    for body in ({}, {"listing_id": "x", "group": "c1", "position": 1}, {"listing_id": 1, "group": "z1"},
                 {"listing_id": 1, "group": "c1", "position": 0}):
        assert consented.post("/api/search/click", json=body, headers=HEADERS).status_code == 204, body
    for raw in (b"not json", b""):
        res = consented.post("/api/search/click", content=raw,
                             headers={**HEADERS, "Content-Type": "application/json"})
        assert res.status_code == 204, raw
    assert events()[0]["clicks"] == before
    assert consented.post("/api/search/click", json={}).status_code == 403  # the guard still comes first
