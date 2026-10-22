"""The "asked for near you" demand report (P9.7; app/services/demand_report.py; plan section 8.19).

The tests use their own verified shop about 110 km from the seeded ones, so no other shop is within the report's
3 km, and their own search rows (the raw query starts with the test's tag); everything they write is removed
afterwards. The demand-key secret is set per test with monkeypatch: the suite runs without one, as production does
until B24 confirms the privacy notice."""
import random
import uuid
from datetime import datetime, timedelta

import pytest

from app import jobs
from app.ai.semantic_index import index
from app.api.routes.auth import record_consents
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.models import Product, Role, SearchEvent, Shop, User
from app.models.billing import Actor
from app.schemas.requests import ConsentIn
from app.services import analytics, demand_report, plans, privacy, search
from database import admin_tasks
from tests.conftest import CENTER, PASSWORD, verified_shop_fields

SECRET = "t" * 40
FAR = (CENTER[0] + 1.0, CENTER[1])  # about 110 km north of the seeded shops
COLLECTIONS = (demand_report.REPORTS, demand_report.AUDITS, demand_report.FAILURES)


def _wipe(db: Database) -> None:
    for name in COLLECTIONS:
        db.mongo[name].delete_many({})


class World:
    def __init__(self, db: Database):
        self.db = db
        # Letters only: a run of 7 digits in a query would read as a phone number and be blocklisted.
        self.tag = "dr" + uuid.uuid4().hex[:8].translate(str.maketrans("0123456789", "ghijklmnop"))
        self.users: list[int] = []
        self.shops: list[int] = []
        owner = self.user(Role.OWNER)
        self.shop = db.shops.insert(Shop(
            owner_id=owner.id, name=f"Demand Shop {self.tag}", slug=f"demand-shop-{self.tag}", address_line="1 Road",
            city="Ballari", lat=FAR[0], lng=FAR[1], category_ids=[], **verified_shop_fields(97)))
        self.shops.append(self.shop.id)
        self.category = db.categories.find_one()

    def user(self, role: Role = Role.CUSTOMER, **consent) -> User:
        u = self.db.users.insert(User(name="Demand Person", email=f"{self.tag}-{uuid.uuid4().hex[:6]}@example.com",
                                      password_hash=hash_password(PASSWORD), role=role))
        self.users.append(u.id)
        if consent:
            record_consents(self.db, u.id, ConsentIn(account_and_orders=True, age_18_plus=True, **consent))
        return u

    def listing(self, name: str, quantity: int, stock_age_days: float = 0) -> Product:
        p = self.db.products.insert(Product(
            shop_id=self.shop.id, category_id=self.category.id, name=name, keywords=name,
            price=100, quantity=quantity, stock_updated_at=utcnow() - timedelta(days=stock_age_days)))
        index.mark_dirty()
        return p

    def searches(self, query: str, keys: list[str | None], in_stock: int = 0, days_ago: float = 1,
                 where: tuple[float, float] | None = None) -> None:
        lat, lng = where or FAR
        self.db.search_history.insert_many([SearchEvent(
            query=f"{self.tag} {query}", normalized_query=query, results_count=0, in_stock_results=in_stock,
            demand_key=k, lat=round(lat, 2), lng=round(lng, 2), created_at=utcnow() - timedelta(days=days_ago))
            for k in keys])

    def cleanup(self) -> None:
        db = self.db
        db.search_history.delete_many({"query": {"$regex": f"^{self.tag}"}})
        db.products.delete_many({"shop_id": {"$in": self.shops}})
        db.shops.delete_many({"_id": {"$in": self.shops}})
        db.mongo["consents"].delete_many({"user_id": {"$in": self.users}})
        db.users.delete_many({"_id": {"$in": self.users}})
        db.mongo[plans.SUBSCRIPTIONS].delete_many({"shop_ids": {"$in": self.shops}})
        db.mongo[plans.EVENTS].delete_many({"shop_ids": {"$in": self.shops}})
        index.mark_dirty()


@pytest.fixture
def world(ids, monkeypatch):
    db = Database()
    monkeypatch.setattr(settings, "demand_key_secret", type(settings.demand_key_secret)(SECRET))
    # Keyword matching only: the suite's TF-IDF stand-in is fitted on the 3 seeded listings (2 dimensions), so any two
    # words it never saw look alike to it, which no real embedding model does.
    monkeypatch.setattr(search, "_vector_stage", lambda *a, **k: lambda ids: {})
    monkeypatch.setattr(index, "query", lambda *a, **k: {})
    _wipe(db)
    w = World(db)
    yield w
    w.cleanup()
    _wipe(db)


def keys(n: int) -> list[str]:
    """n distinct demand keys, as different people's searches would carry."""
    return [search.demand_key_of(10_000_000 + random.randrange(10**9)) for _ in range(n)]


def build(world: World) -> tuple[list[dict], dict[str, int], list[dict]]:
    return demand_report.build_shop(world.db, world.shop, utcnow() - timedelta(days=30))


def queries(items: list[dict]) -> list[str]:
    return [i["query"] for i in items]


# ---------------------------------------------------------------- distinct people


def test_four_people_do_not_make_an_item_five_do_and_repeats_and_signed_out_searches_count_once(world):
    t = world.tag
    world.searches(f"four people {t}", keys(4))
    world.searches(f"five people {t}", keys(5))
    one = keys(1)
    world.searches(f"one person often {t}", one * 20 + keys(3))  # 20 searches by one person and 3 others: 4
    world.searches(f"signed out {t}", keys(4) + [None] * 10)  # signed out never counts
    world.searches(f"found in stock {t}", keys(4))
    world.searches(f"found in stock {t}", keys(3), in_stock=2)  # these found it in stock: they never count
    world.searches(f"too far {t}", keys(5), where=(FAR[0] + 0.05, FAR[1]))  # about 5.5 km away
    world.searches(f"too old {t}", keys(5), days_ago=31)
    items, _, _ = build(world)
    assert queries(items) == [f"five people {t}"]
    assert items[0]["band"] == "5-9"

    world.searches(f"one person often {t}", keys(1))  # a fifth person
    assert f"one person often {t}" in queries(build(world)[0])


@pytest.mark.parametrize(("people", "label"), [(4, None), (5, "5-9"), (9, "5-9"), (10, "10-19"), (19, "10-19"),
                                               (20, "20+"), (250, "20+")])
def test_bands(people, label):
    assert demand_report.band(people) == label


def test_bands_in_a_built_report(world):
    t = world.tag
    for n in (9, 10, 19, 20):
        world.searches(f"band {n} {t}", keys(n))
    items, _, _ = build(world)
    assert [(i["query"], i["band"]) for i in items] == [
        (f"band 20 {t}", "20+"), (f"band 19 {t}", "10-19"), (f"band 10 {t}", "10-19"), (f"band 9 {t}", "5-9")]


# ---------------------------------------------------------------- drops


def test_blocklist_length_and_failed_normalisation_are_dropped(world):
    t = world.tag
    bad = ["call 98450 12345", "ravi gmail.com", "x", "a" * 61, "123 / 456", "...", "fucking charger"]
    for q in bad:
        world.searches(q, keys(5))
    world.searches(f"fine query {t}", keys(5))
    assert demand_report.drop_reason("a" * 60) is None and demand_report.drop_reason("ok") is None
    items, dropped, _ = build(world)
    assert queries(items) == [f"fine query {t}"]
    assert dropped == {"failed": 2, "length": 2, "blocklist": 3, "recheck": 0, "shop_stocks": 0}


def test_the_recheck_drops_what_is_now_in_stock_and_a_match_in_the_shops_own_stock_is_a_search_failure(world):
    world.listing("copper kettle", quantity=4)  # in stock and fresh: search finds it now
    stale = world.listing("brass lantern", quantity=6, stock_age_days=30)  # in stock, but not confirmed for 30 days
    world.listing("steel tiffin", quantity=0)  # out of stock: the item is kept, matched to this category
    world.searches("copper kettle", keys(5))
    world.searches("brass lantern", keys(6))
    world.searches("steel tiffin", keys(7))
    items, dropped, failures = build(world)
    assert queries(items) == ["steel tiffin"]
    assert items[0]["category"] == world.category.slug and items[0]["band"] == "5-9"
    assert dropped["recheck"] == 1 and dropped["shop_stocks"] == 1
    assert failures == [{"query": "brass lantern", "product_ids": [stale.id]}]

    # The weekly build records the failure for R5, and the demand-audit command shows it.
    result = demand_report.build_week(world.db, rng=random.Random(1))
    assert result["search_failures"] >= 1
    lines: list[str] = []
    assert admin_tasks.demand_audit_failures(world.db, out=lines.append) == 0
    assert any(f"shop {world.shop.id}" in line and "brass lantern" in line and str(stale.id) in line
               for line in lines)


# ---------------------------------------------------------------- the demand key


def test_the_demand_key_is_removed_after_30_days(world):
    t = world.tag
    world.searches(f"old key {t}", keys(1), days_ago=31)
    world.searches(f"new key {t}", keys(1), days_ago=29)
    result = privacy.run_retention(world.db, utcnow())
    assert result["demand_keys_removed"] >= 1
    rows = {r["normalized_query"]: r["demand_key"]
            for r in world.db.search_history.find_raw({"query": {"$regex": f"^{t}"}}, {"normalized_query": 1,
                                                                                        "demand_key": 1})}
    assert rows[f"old key {t}"] is None and rows[f"new key {t}"] is not None


def test_the_demand_key_is_stored_only_signed_in_and_with_the_consent_when_the_setting_requires_it(world,
                                                                                                    monkeypatch):
    t = world.tag
    with_consent = world.user(search_measurement=True)
    without = world.user(search_measurement=False)
    p = search.SearchParams(q=f"{t} probe", lat=FAR[0], lng=FAR[1])

    def logged(user_id: int | None) -> dict:
        search.log_search(world.db, user_id, p, f"probe {t}", 0, 0)
        return world.db.search_history.find_raw({"query": f"{t} probe"}, sort=[("_id", -1)], limit=1)[0]

    monkeypatch.setattr(settings, "demand_key_needs_consent", True)
    assert logged(without.id)["demand_key"] is None
    assert logged(None)["demand_key"] is None  # signed out: never
    row = logged(with_consent.id)
    assert row["demand_key"] == search.demand_key_of(with_consent.id) and len(row["demand_key"]) == 64
    assert row["demand_key"] != str(with_consent.id) and row["in_stock_results"] == 0

    monkeypatch.setattr(settings, "demand_key_needs_consent", False)  # if B24 says no separate consent is needed
    assert logged(without.id)["demand_key"] == search.demand_key_of(without.id)
    assert logged(None)["demand_key"] is None

    monkeypatch.setattr(settings, "demand_key_secret", type(settings.demand_key_secret)(""))
    assert logged(without.id)["demand_key"] is None  # no secret, no key

    # Withdrawing the consent removes the user's key from earlier searches at once.
    monkeypatch.setattr(settings, "demand_key_secret", type(settings.demand_key_secret)(SECRET))
    monkeypatch.setattr(settings, "demand_key_needs_consent", True)
    privacy.set_consent(world.db, with_consent.id, "search_measurement", False)
    assert world.db.search_history.count({"demand_key": search.demand_key_of(with_consent.id)}) == 0


def test_a_search_logs_its_in_stock_results_inside_the_radius(world, monkeypatch):
    t = world.tag
    world.listing("garden hose", quantity=3)
    world.listing("garden hose reel", quantity=0)
    monkeypatch.setattr(world.db, "background", lambda fn: fn())
    user = world.user(search_measurement=True)
    found = search.run_search(world.db, search.SearchParams(q=f"garden hose {t}", lat=FAR[0], lng=FAR[1],
                                                            radius_km=3), user_id=user.id)
    assert found["total"] >= 1
    row = world.db.search_history.find_raw({"normalized_query": f"garden hose {t}"}, sort=[("_id", -1)], limit=1)[0]
    assert row["in_stock_results"] == 1 and row["demand_key"] == search.demand_key_of(user.id)
    row["query"] = f"{t} {row['query']}"  # so cleanup removes it
    world.db.search_history.update_one({"_id": row["_id"]}, {"$set": {"query": row["query"]}})


# ---------------------------------------------------------------- showing


def audits(db: Database, week: str, genuine: int, total: int = 20, labelled: int | None = None) -> None:
    labelled = total if labelled is None else labelled
    ids_ = db.next_ids(demand_report.AUDITS, total)
    db.mongo[demand_report.AUDITS].insert_many([
        {"_id": i, "week": week, "shop_id": 1, "query": f"audit {n}", "catalog_item_id": None, "item_name": None,
         "category": None, "band": "5-9",
         "label": None if n >= labelled else ("genuine" if n < genuine else "junk"),
         "labelled_at": None if n >= labelled else utcnow()} for n, i in enumerate(ids_)])


def report_row(world: World, week: str, n_items: int) -> None:
    world.db.mongo[demand_report.REPORTS].insert_one({
        "_id": world.db.next_id(demand_report.REPORTS), "shop_id": world.shop.id, "week": week,
        "items": [{"query": f"wanted {n}", "catalog_item_id": None, "item_name": None, "category": "plumbing",
                   "band": "5-9"} for n in range(n_items)], "dropped": dict.fromkeys(demand_report.DROP_REASONS, 0)})


def test_disabled_held_and_shown_states_each_with_its_reason(world, monkeypatch):
    db, shop = world.db, world.shop
    report_row(world, "2999-W01", 5)

    monkeypatch.setattr(settings, "demand_report_enabled", False)
    m = demand_report.owner_module(db, shop)
    assert m["items"] == [] and m["gate"]["code"] == "disabled" and "not switched on" in m["gate"]["reason"]

    monkeypatch.setattr(settings, "demand_report_enabled", True)
    for week, genuine in (("2001-W01", 20), ("2001-W02", 18), ("2001-W03", 16)):
        audits(db, week, genuine)
    audits(db, "2001-W04", 20, labelled=19)  # not fully labelled: does not count yet
    m = demand_report.owner_module(db, shop)
    assert m["items"] == [] and m["gate"]["code"] == "audit_pending"
    assert m["gate"]["reason"] == "The report is on hold while NearShop checks it: 3 of 4 weekly checks done"

    db.mongo[demand_report.AUDITS].update_many({"week": "2001-W04", "label": None}, {"$set": {"label": "junk"}})
    audits(db, "2001-W05", 10)  # the last 4 complete: 50, 95 (19 of 20), 80, 90 -> 78.8%
    m = demand_report.owner_module(db, shop)
    assert m["items"] == [] and m["gate"]["code"] == "precision"
    assert m["gate"]["reason"] == ("The report is on hold while NearShop improves it: the last 4 weekly checks found "
                                   "78.8% of items genuine, and 80% is needed")
    lines: list[str] = []
    admin_tasks.demand_audit_status(db, out=lines.append)  # admins see why
    assert any("HELD (precision)" in line for line in lines)

    audits(db, "2001-W06", 20)  # 100, 50, 95, 80 -> 81.2%
    m = demand_report.owner_module(db, shop)
    assert m["gate"]["shown"] and m["gate"]["reason"] is None
    assert [i["query"] for i in m["items"]] == ["wanted 0", "wanted 1", "wanted 2"]  # Free: the teaser, top 3
    assert m["plan"] == "teaser" and m["hidden"] == 2 and m["week"] == "2999-W01"

    plans.create_subscription(db, shop_ids=[shop.id], plan_code="growth", period="year", source="guarantee_credit",
                              actor=Actor(type="admin", id=0), reason="Demand report test grant", months=12)
    m = demand_report.owner_module(db, shop)
    assert len(m["items"]) == 5 and m["plan"] == "full" and m["hidden"] == 0

    # owner_insights takes its missed-demand module from the report, never raw zero-result counts.
    out = analytics.owner_insights(db, db.shops.get(shop.id))
    assert out["missed_demand"] == m["items"] and out["gates"]["missed_demand"]["shown"]
    assert set(out["missed_demand"][0]) == {"query", "item", "category", "band"}

    db.mongo[demand_report.REPORTS].delete_many({"shop_id": shop.id})
    m = demand_report.owner_module(db, shop)  # no row for this shop in the latest week
    assert m["items"] == [] and m["gate"]["code"] == "no_report"


# ---------------------------------------------------------------- what a report row may hold


def test_no_report_row_holds_a_user_id_a_demand_key_a_time_or_a_location(world, monkeypatch):
    t = world.tag
    people = keys(12)
    world.searches(f"pressure cooker {t}", people)
    world.listing("pressure cooker", quantity=0)
    result = demand_report.build_week(world.db, rng=random.Random(7))
    assert result["kept"] >= 1 and result["audit_sampled"] >= 1
    rows = list(world.db.mongo[demand_report.REPORTS].find({})) + list(world.db.mongo[demand_report.AUDITS].find({}))
    mine = world.db.mongo[demand_report.REPORTS].find_one({"shop_id": world.shop.id})
    assert [(i["query"], i["band"]) for i in mine["items"]] == [(f"pressure cooker {t}", "10-19")]
    forbidden = set(people) | {str(u) for u in world.users}
    banned_fields = {"user_id", "user_ids", "demand_key", "demand_keys", "lat", "lng", "location", "created_at"}

    def walk(value, path=""):
        if isinstance(value, dict):
            for k, v in value.items():
                assert k not in banned_fields, f"{path}.{k}"
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for v in value:
                walk(v, path)
        else:
            assert not isinstance(value, datetime) or path.endswith("labelled_at"), path  # R5's label time only
            assert value not in forbidden, path

    for row in rows:
        walk(row)
    report_rows = list(world.db.mongo[demand_report.REPORTS].find({}))
    assert report_rows and all(not isinstance(v, datetime) for r in report_rows for v in r.values())

    # The owner's view of it holds no more than the row.
    monkeypatch.setattr(settings, "demand_report_enabled", True)
    for n in range(4):
        audits(world.db, f"2000-W0{n + 1}", 20)
    world.db.mongo[demand_report.AUDITS].update_many({"label": None}, {"$set": {"label": "genuine",
                                                                               "labelled_at": utcnow()}})
    m = demand_report.owner_module(world.db, world.shop)
    assert m["items"] and all(set(i) == {"query", "item", "category", "band"} for i in m["items"])
    walk(m["items"])


# ---------------------------------------------------------------- the job and the audit command


def test_the_weekly_job_runs_once_per_week_from_monday_0300_ist(world):
    monday = datetime(2027, 5, 17, 3, 0)  # IST wall clock
    slot = jobs.ist_midnight_utc(monday.date()) + timedelta(hours=3)
    assert demand_report.weekly_slot(slot) == slot
    assert demand_report.weekly_slot(slot - timedelta(seconds=1)) == slot - timedelta(days=7)
    assert demand_report.weekly_slot(slot + timedelta(days=6, hours=23)) == slot
    assert demand_report.week_of(slot) == "2027-W20"

    db = world.db
    db.mongo[jobs.LEASES].delete_one({"_id": jobs.DEMAND_REPORT_JOB})
    clock = {"now": utcnow()}
    scheduler = jobs.Scheduler(db.mongo, jobs={jobs.DEMAND_REPORT_JOB: lambda: jobs.demand_report(lambda: clock["now"])},
                               holder="demand-test", clock=lambda: clock["now"])
    built = []
    real = demand_report.build_week
    demand_report.build_week = lambda *a, **k: built.append(1) or real(*a, **k)
    try:
        assert scheduler.tick() == {jobs.DEMAND_REPORT_JOB: True}
        clock["now"] += timedelta(minutes=1)
        scheduler.tick()
        lease = db.mongo[jobs.LEASES].find_one({"_id": jobs.DEMAND_REPORT_JOB})["last_result"]
        assert lease["ok"] and lease["ran_for"] == demand_report.week_of(demand_report.weekly_slot(clock["now"]))
        assert built == [1]  # the second tick of the week only repeats the result
        clock["now"] = demand_report.weekly_slot(clock["now"]) + timedelta(days=7)
        scheduler.tick()
        assert built == [1, 1]
    finally:
        demand_report.build_week = real
        scheduler.shutdown()
        db.mongo[jobs.LEASES].delete_one({"_id": jobs.DEMAND_REPORT_JOB})


def test_r5_labels_the_audit_sample_one_at_a_time(world):
    db = world.db
    audits(db, "2002-W01", 0, total=3, labelled=0)
    answers = iter([db.mongo.name, "g", "x", "s", "j", "q"])
    lines: list[str] = []
    assert admin_tasks.main(["demand-audit", "label"], ask=lambda _: next(answers), out=lines.append) == 0
    labels = [r["label"] for r in db.mongo[demand_report.AUDITS].find({"week": "2002-W01"}, sort=[("_id", 1)])]
    assert labels == ["genuine", None, "junk"]  # the second was skipped
    assert "Type g, f, j, s or q." in lines and "Labelled 2 item(s)." in lines

    refused = iter(["wrong name"])
    assert admin_tasks.demand_audit_label(db, ask=lambda _: next(refused), out=lines.append) == 1
    with pytest.raises(ValueError):
        demand_report.record_label(db, 1, "maybe")
