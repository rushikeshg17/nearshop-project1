"""The "asked for near you" demand report (P9.7; plan section 8.19; business model C4).

Each shop sees what people nearby searched for and could not find in stock, counted so that NearShop's own search
failures, one person's repeated searches and anything that could identify a customer never reach it.

What search logs
----------------
Every search_history row (app/services/search.py log_search) carries in_stock_results, the in-stock listings the
search found inside its radius, and, for a signed-in user only, demand_key: HMAC-SHA256 of the user id with
NEARSHOP_DEMAND_KEY_SECRET (search.demand_key_of). With demand_key_needs_consent on (the default until B24 decides)
the key is stored only for users whose latest consent for demand_key_consent_purpose is granted. Signed-out searches
never get one, and with the secret unset nobody does. The nightly retention job removes the key after 30 days
(app/services/privacy.py run_retention); withdrawing that consent or deleting the account removes it at once.

The weekly build
----------------
The demand_report job (app/jobs.py), Monday at 03:00 IST under its lease, runs build_week: for every verified live
shop, the searches of the last demand_report_window_days (30) days within demand_report_radius_km (3 km) of the shop
are grouped by normalised query, counting only searches that found no in-stock result, and a query is a candidate
when demand_report_min_users (5) or more distinct demand keys searched it. A candidate is then dropped, and counted
in the row's dropped counts under its reason, when:

  failed        normalisation left no letter of any script (normalization_failed);
  length        it is shorter than 2 or longer than 60 characters;
  blocklist     it holds a phone number, email address, link or abusive word (the P5.5 blocklist, search.blocklisted);
  recheck       the same search from the shop's location with the in-stock filter now finds an in-stock result (a
                result search returns has passed its relevance cutoffs);
  shop_stocks   the shop itself has an in-stock listing that matches (stocks_match, the check owner insights makes
                for "you stock it"): the query goes to demand_search_failures, the possible search failures that
                the admin_tasks demand-audit command shows R5.

Each kept item stores the query, the catalogue item and category the search matches among out-of-stock listings
when there is one, and its band of distinct people: 5-9, 10-19 or 20+. The result is one demand_reports row per shop
per ISO week (week_of the Monday), replaced by a re-run. Nothing in a row is a user id, a demand key, a time or a
location: the week names the report, not when anyone searched.

The audit gate
--------------
Each week build_week samples demand_report_audit_size (20) kept items city-wide into demand_audits (once per week).
R5 labels each with the demand-audit command: genuine (unmet demand), search_failure or junk. A week's audit counts
once all its items are labelled; its precision is the genuine share.

Showing
-------
report_gate: nothing is shown while demand_report_enabled is off (R5 turns it on only once checks 8.2 and 8.3 are
recorded as passing in docs/search/tuning-report.md); when on, reports are held unless the last
demand_report_audit_weeks (4) complete audits average demand_report_min_precision_pct (80%) genuine or more. The
gate's reason says why (the owner sees it on the insights page; admins see it with demand-audit status). Shown,
owner_module gives a Free shop the top demand_report_teaser_items (3) items of its latest report (the plan's teaser
entitlement) and Growth and above every item (full).
"""
from __future__ import annotations

import logging
import random
import unicodedata
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.database import Database, utcnow
from app.models import Shop
from app.models.core import live_shop_filter
from app.utils.geo import within_km

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
REPORTS = "demand_reports"
AUDITS = "demand_audits"
FAILURES = "demand_search_failures"
WEEKLY_AT = time(3, 0)  # IST, on Mondays
MIN_CHARS, MAX_CHARS = 2, 60  # plan section 8.19: 60 characters or fewer; P5.5's limit
# Bands of distinct people: (lowest count, label). A count below the first is never kept.
BAND_FLOORS: tuple[tuple[int, str], ...] = ((20, "20+"), (10, "10-19"), (5, "5-9"))
DEMAND_BANDS: tuple[str, ...] = ("5-9", "10-19", "20+")
DEMAND_AUDIT_LABELS: tuple[str, ...] = ("genuine", "search_failure", "junk")
DROP_REASONS: tuple[str, ...] = ("failed", "length", "blocklist", "recheck", "shop_stocks")
SEMANTIC_MATCH = 0.5  # stocks_match: the similarity owner insights has used for "you stock it"


# ---------------------------------------------------------------- pure rules


def band(distinct_people: int) -> str | None:
    """The band of a count of distinct people: 5-9, 10-19 or 20+; None below 5."""
    return next((label for floor, label in BAND_FLOORS if distinct_people >= floor), None)


def normalization_failed(norm: str) -> bool:
    """Whether normalisation (app/utils/text.py normalize_query) failed to leave a searchable query: nothing, or no
    letter of any script (only digits and punctuation, such as a number or "..." once symbols are dropped)."""
    return not any(unicodedata.category(c)[0] == "L" for c in norm or "")


def drop_reason(norm: str) -> str | None:
    """Why a query's text may never be in a report (failed, length or blocklist), or None."""
    from app.services.search import blocklisted

    if normalization_failed(norm):
        return "failed"
    if not MIN_CHARS <= len(norm.strip()) <= MAX_CHARS:
        return "length"
    if blocklisted(norm):
        return "blocklist"
    return None


def ist_date(at: datetime):
    return at.replace(tzinfo=UTC).astimezone(IST).date()


def week_of(at: datetime) -> str:
    """The ISO week (YYYY-Www) of a naive UTC time's IST date."""
    year, week, _ = ist_date(at).isocalendar()
    return f"{year}-W{week:02d}"


def weekly_slot(now: datetime) -> datetime:
    """The most recent Monday 03:00 IST at or before now, as a naive UTC time."""
    day = ist_date(now)
    monday = day - timedelta(days=day.weekday())
    slot = datetime.combine(monday, WEEKLY_AT, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)
    return slot if slot <= now else slot - timedelta(days=7)


# ---------------------------------------------------------------- the build


def matching_ids(db: Database, in_stock_ids: set[int], q: str) -> set[int]:
    """The listings of in_stock_ids (a shop's in-stock listings) that match the query: a keyword hit, or a semantic
    similarity above SEMANTIC_MATCH. The check owner insights makes for "you stock it"."""
    from app.ai.semantic_index import index
    from app.services.search import keyword_scores
    from app.utils.text import normalize_query

    if not in_stock_ids:
        return set()
    kw = keyword_scores(db, normalize_query(q).split())
    sem = index.query(db, q)
    return {pid for pid in in_stock_ids if pid in kw or sem.get(pid, 0) > SEMANTIC_MATCH}


def stocks_match(db: Database, in_stock_ids: set[int], q: str) -> bool:
    return bool(matching_ids(db, in_stock_ids, q))


def candidates(db: Database, shop: Shop, since: datetime) -> list[tuple[str, int]]:
    """(normalised query, distinct demand keys) of the searches since `since` within the report radius of the shop
    that found no in-stock result, for queries demand_report_min_users or more distinct keys searched; most people
    first. Searches without a demand key (signed out, no consent, older than 30 days) never count."""
    rows = db.search_history.aggregate([
        {"$match": {"created_at": {"$gte": since}, "demand_key": {"$type": "string"}, "in_stock_results": 0,
                    "location": within_km(shop.lat, shop.lng, settings.demand_report_radius_km)}},
        {"$group": {"_id": "$normalized_query", "keys": {"$addToSet": "$demand_key"}}},
        {"$project": {"_id": 1, "n": {"$size": "$keys"}}},
        {"$match": {"n": {"$gte": settings.demand_report_min_users}}},
        {"$sort": {"n": -1, "_id": 1}},
    ])
    return [(r["_id"], int(r["n"])) for r in rows if isinstance(r["_id"], str)]


def _search(db: Database, shop: Shop, q: str, in_stock_only: bool) -> dict:
    from app.services.search import SearchParams, run_search

    return run_search(db, SearchParams(q=q, lat=shop.lat, lng=shop.lng, radius_km=settings.demand_report_radius_km,
                                       in_stock_only=in_stock_only, page_size=1), log=False)


def _matched(found: dict) -> dict[str, Any]:
    """The catalogue item and category of a search's top group (out-of-stock listings near the shop), if any."""
    group = (found.get("groups") or [None])[0]
    if not group:
        return {"catalog_item_id": None, "item_name": None, "category": None}
    item = group.get("catalog_item_id")
    return {"catalog_item_id": item, "item_name": group.get("name") if item else None,
            "category": group.get("category")}


def build_shop(db: Database, shop: Shop, since: datetime) -> tuple[list[dict], dict[str, int], list[dict]]:
    """One shop's report: (kept items, dropped counts by reason, possible search failures)."""
    dropped = dict.fromkeys(DROP_REASONS, 0)
    items: list[dict] = []
    failures: list[dict] = []
    in_stock_ids = {p["_id"] for p in db.products.find_raw(
        {"shop_id": shop.id, "is_active": True, "quantity": {"$gt": 0}}, {"_id": 1})}
    for query, people in candidates(db, shop, since):
        reason = drop_reason(query)
        if reason is None and _search(db, shop, query, in_stock_only=True)["total"] > 0:
            reason = "recheck"
        if reason is None and (matched := matching_ids(db, in_stock_ids, query)):
            reason = "shop_stocks"
            failures.append({"query": query, "product_ids": sorted(matched)[:20]})
        if reason is not None:
            dropped[reason] += 1
            continue
        label = band(people)
        if label is None:  # cannot happen with min_users of 5 or more; never keep a smaller count
            continue
        items.append({"query": query, **_matched(_search(db, shop, query, in_stock_only=False)), "band": label})
    return items, dropped, failures


def build_week(db: Database, now: datetime | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    """Build this week's report for every verified live shop (the module docstring), write one demand_reports row per
    shop (a re-run replaces it), record the possible search failures, and draw the week's audit sample once. Returns
    counts only."""
    now = now or utcnow()
    week = week_of(weekly_slot(now))
    since = now - timedelta(days=settings.demand_report_window_days)
    shops = db.shops.find(live_shop_filter(now), sort=[("_id", 1)])
    kept: list[tuple[int, dict]] = []
    totals = dict.fromkeys(DROP_REASONS, 0)
    failures_n = 0
    for shop in shops:
        items, dropped, failures = build_shop(db, shop, since)
        for reason, n in dropped.items():
            totals[reason] += n
        _store_report(db, shop.id, week, items, dropped)
        for f in failures:
            db.mongo[FAILURES].update_one(
                {"week": week, "shop_id": shop.id, "query": f["query"]},
                {"$set": {"product_ids": f["product_ids"]}, "$setOnInsert": {"_id": db.next_id(FAILURES)}},
                upsert=True)
        failures_n += len(failures)
        kept += [(shop.id, item) for item in items]
    sampled = _sample_audits(db, week, kept, rng or random.Random())
    log.info("demand report %s: %d shops, %d items kept, %d possible search failures, %d audit items drawn",
             week, len(shops), len(kept), failures_n, sampled)
    return {"week": week, "shops": len(shops), "kept": len(kept), "search_failures": failures_n,
            "audit_sampled": sampled, "dropped": totals}


def _store_report(db: Database, shop_id: int, week: str, items: list[dict], dropped: dict[str, int]) -> None:
    """Write the shop's row for the week, replacing a re-run's (a skipped id after a re-run is harmless)."""
    db.mongo[REPORTS].update_one({"shop_id": shop_id, "week": week},
                                 {"$set": {"items": items, "dropped": dropped},
                                  "$setOnInsert": {"_id": db.next_id(REPORTS)}}, upsert=True)


def _sample_audits(db: Database, week: str, kept: list[tuple[int, dict]], rng: random.Random) -> int:
    """Draw the week's audit sample from every kept item city-wide, once: a re-run of the week draws nothing."""
    coll = db.mongo[AUDITS]
    if not kept or coll.find_one({"week": week}, {"_id": 1}) is not None:
        return 0
    chosen = rng.sample(kept, min(settings.demand_report_audit_size, len(kept)))
    ids = db.next_ids(AUDITS, len(chosen))
    coll.insert_many([{"_id": i, "week": week, "shop_id": shop_id, "query": item["query"],
                       "catalog_item_id": item.get("catalog_item_id"), "item_name": item.get("item_name"),
                       "category": item.get("category"), "band": item["band"], "label": None, "labelled_at": None}
                      for i, (shop_id, item) in zip(ids, chosen, strict=True)])
    return len(chosen)


# ---------------------------------------------------------------- the audit gate


def audit_weeks(db: Database) -> list[dict[str, Any]]:
    """Every audited week, newest first: week, items, labelled, genuine, complete and precision_pct (the genuine
    share of a complete week, else None)."""
    rows = db.mongo[AUDITS].aggregate([
        {"$group": {"_id": "$week", "items": {"$sum": 1},
                    "labelled": {"$sum": {"$cond": [{"$ne": ["$label", None]}, 1, 0]}},
                    "genuine": {"$sum": {"$cond": [{"$eq": ["$label", "genuine"]}, 1, 0]}}}},
        {"$sort": {"_id": -1}},
    ])
    out = []
    for r in rows:
        complete = r["labelled"] == r["items"]
        out.append({"week": r["_id"], "items": r["items"], "labelled": r["labelled"], "genuine": r["genuine"],
                    "complete": complete,
                    "precision_pct": round(100 * r["genuine"] / r["items"], 1) if complete and r["items"] else None})
    return out


def _gate(shown: bool, code: str | None, reason: str | None, progress: list[dict] | None = None) -> dict:
    """The model-gate shape of app/services/analytics.py (gate)."""
    return {"shown": shown, "code": None if shown else code, "reason": None if shown else reason,
            "progress": progress or []}


def report_gate(db: Database) -> dict[str, Any]:
    """Whether reports may be shown now (the module docstring), with the reason when not, and audit details."""
    weeks_needed = settings.demand_report_audit_weeks
    need_pct = settings.demand_report_min_precision_pct
    if not settings.demand_report_enabled:
        return {**_gate(False, "disabled", "The report of what people nearby could not find is not switched on yet: "
                                           "NearShop is still checking that its search finds what shops stock"),
                "audits": [], "average_pct": None}
    complete = [w for w in audit_weeks(db) if w["complete"]][:weeks_needed]
    if len(complete) < weeks_needed:
        return {**_gate(False, "audit_pending",
                        f"The report is on hold while NearShop checks it: {len(complete)} of {weeks_needed} weekly "
                        "checks done", [{"label": "weekly checks", "have": len(complete), "need": weeks_needed}]),
                "audits": complete, "average_pct": None}
    average = round(sum(w["precision_pct"] for w in complete) / len(complete), 1)
    progress = [{"label": "% genuine", "have": average, "need": need_pct}]
    if average < need_pct:
        return {**_gate(False, "precision",
                        f"The report is on hold while NearShop improves it: the last {weeks_needed} weekly checks found "
                        f"{average:g}% of items genuine, and {need_pct:g}% is needed", progress),
                "audits": complete, "average_pct": average}
    return {**_gate(True, None, None, progress), "audits": complete, "average_pct": average}


def latest_week(db: Database) -> str | None:
    row = db.mongo[REPORTS].find_one({}, {"week": 1}, sort=[("week", -1)])
    return row["week"] if row else None


def owner_module(db: Database, shop: Shop, now: datetime | None = None) -> dict[str, Any]:
    """The owner insights' missed-demand module (P7.3 gating): {items, gate, week, plan, hidden}. Items come from the
    shop's row in the latest week's reports: the top demand_report_teaser_items on Free (the teaser entitlement), every
    item on Growth and above (full). A disabled or held report, or no row this week, gives no items and its reason."""
    from app.services import plans

    gate = report_gate(db)
    base = {"items": [], "week": None, "plan": None, "hidden": 0}
    if not gate["shown"]:
        return {**base, "gate": _strip(gate)}
    week = latest_week(db)
    row = db.mongo[REPORTS].find_one({"shop_id": shop.id, "week": week}, {"items": 1}) if week else None
    if row is None:
        return {**base, "gate": _gate(False, "no_report", "No report for your shop yet: reports are made every Monday "
                                                          "morning")}
    entitlement = plans.entitled(db, shop.id, "demand_report", now)
    items = [{"query": i["query"], "item": ({"id": i["catalog_item_id"], "name": i.get("item_name")}
                                            if i.get("catalog_item_id") else None),
              "category": i.get("category"), "band": i["band"]} for i in row.get("items") or []]
    shown = items if entitlement == "full" else items[:settings.demand_report_teaser_items]
    return {"items": shown, "week": week, "plan": "full" if entitlement == "full" else "teaser",
            "hidden": len(items) - len(shown), "gate": _strip(gate)}


def _strip(gate: dict) -> dict:
    return {k: gate[k] for k in ("shown", "code", "reason", "progress")}


# ---------------------------------------------------------------- R5's audit and the search-failure list


def next_unlabelled(db: Database) -> dict | None:
    """The oldest week's next unlabelled audit item, or None."""
    return db.mongo[AUDITS].find_one({"label": None}, sort=[("week", 1), ("_id", 1)])


def record_label(db: Database, audit_id: int, label: str, now: datetime | None = None) -> bool:
    """Label one audit item (genuine, search_failure or junk). False when it does not exist or is labelled already."""
    if label not in DEMAND_AUDIT_LABELS:
        raise ValueError(f"label must be one of {', '.join(DEMAND_AUDIT_LABELS)}")
    return db.mongo[AUDITS].update_one({"_id": audit_id, "label": None},
                                       {"$set": {"label": label, "labelled_at": now or utcnow()}}).modified_count == 1


def search_failures(db: Database, limit: int = 100) -> list[dict]:
    """The possible search failures, newest week first."""
    return list(db.mongo[FAILURES].find({}, sort=[("week", -1), ("shop_id", 1), ("query", 1)], limit=limit))
