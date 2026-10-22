"""Search interaction logging, online metrics and shadow ranking (P5.6; plan sections 8.4 and 8.6).

Who is measured
---------------
Only signed-in customers whose latest consents row for search_measurement is granted (P3.1; the account page
toggles it, P3.2). Signed-out visitors, owners, admins and run_search callers with log false (the evaluation
harness, the AI Lab comparison) write nothing. The consent is read on every request, so a withdrawal takes
effect on the next one.

What is stored
--------------
One search_events document per search with a query (database/schema.py has its validator, which allows no other
field): a random search id, the time, the normalised query (only when it is 60 characters or fewer and passes
the P5.5 blocklist; otherwise empty, with redacted set), a language tag, the category, sort, filters and radius,
a location at 2 decimals, the result count, whether the query was auto-corrected, the ranking config version, the
impressions (group key, best-offer listing id and 1-based position, at most the first 60 positions), the clicks
and attributed conversions, the previous search id when the trail's previous search is under 2 minutes old, and
for a shadowed search both rankings. Never a user id, session id, IP address, user agent, phone, email, or
reservation or order id. A TTL index deletes an event 90 days after creation.

The trail
---------
The search route keeps a trail in the cookie nearshop_search_trail (HttpOnly, SameSite Lax, Secure as the session
cookie, path /api, 2 hours): the 5 latest search ids and the 5 latest clicks, signed with an HMAC of the secret
key so it cannot be edited. It is set only on a search that is measured. A visitor who is not measured and sends
one (after withdrawing the consent, or signing out) has it deleted, and nothing is recorded from it. A page-1
search with a query starts a new search; a later page with the same query and filters adds its impressions to the
trail's latest search. A click (POST /api/search/click) is added to the most recent trail search whose impressions
hold its group. A reservation or order is attributed to the latest trail click, within the last 2 hours, on its
listing, a listing in the order, or another listing of the same catalogue item.

Off the request path
--------------------
Every write here and the shadow ranking run through db.background, as search_history logging does, and never
fail a search, a click, a reservation or an order.

Shadow mode
-----------
With NEARSHOP_SEARCH_SHADOW_RANKING_VERSION set and NEARSHOP_SEARCH_SHADOW_SHARE above 0, that share of the
measured page-1 searches with a query is re-ranked after the response, on the same fetched listings and scores
(run_search's cache), with the production config and with the candidate. Both top 20s are stored on the event,
under a label naming the config version and the keyword and vector engines (P5.7 adds engine candidates).
Customers always get the production ranking.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import re
import secrets
import statistics
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request, Response
from rapidfuzz import fuzz

from app.core.config import RankingConfig, settings
from app.core.database import Database, utcnow
from app.models import ModelRun, Role, User
from app.schemas.serializers import iso
from app.services.loaders import category_by_slug
from app.services.search import SearchParams, _local_term_candidates, run_search, search_now, suggestion_allowed
from app.utils.text import normalize_query

log = logging.getLogger(__name__)

COLLECTION = "search_events"
CONSENT_PURPOSE = "search_measurement"
TRAIL_COOKIE = "nearshop_search_trail"
TRAIL_SECONDS = 2 * 60 * 60  # the trail cookie's lifetime, and the attribution window
TRAIL_SEARCHES = 5
TRAIL_CLICKS = 5
PREVIOUS_SEARCH_SECONDS = 2 * 60  # a search links to the trail's previous one when that is under 2 minutes old
REFORMULATION_SECONDS = 2 * 60
SUCCESS_SECONDS = 2 * 60 * 60  # a conversion counts towards search success within 2 hours of the search
QUERY_MAX_CHARS = 60
MAX_IMPRESSIONS = 60  # positions 1 to 60
MAX_CLICKS = MAX_CONVERSIONS = 50  # per search event (the validator's limit)
SHADOW_TOP = 20
OVERLAP_TOP = 10
LOCATION_DECIMALS = 2
MIN_LANGUAGE_SEARCHES = 100  # a language's figures appear only once it has this many searches in the window
WINDOWS_DAYS = (7, 30)
SNAPSHOT_NAME = "search_online"
KEYWORD_ENGINE = "mongodb-text-index"
# Shadow guardrails (plan section 8.6).
GUARDRAIL_MIN_DAYS = 7
GUARDRAIL_MIN_SEARCHES = 300
GUARDRAIL_MAX_ZERO_PP = 0.5
GUARDRAIL_MIN_CLICKED_IN_TOP10 = 0.9

# Language tags, in display order. "other" is a query in a script that is neither Latin nor one of the three.
LANGUAGES = ("en", "kn", "kn-Latn", "te", "te-Latn", "hi", "hi-Latn", "other")
SCRIPT_LANGUAGES = {"KANNADA": "kn", "TELUGU": "te", "DEVANAGARI": "hi"}
SCRIPT_RANGES = (("kn", 0x0C80, 0x0CFF), ("te", 0x0C00, 0x0C7F), ("hi", 0x0900, 0x097F))

now: Callable[[], datetime] = utcnow  # tests replace it to move the clock
sample: Callable[[], float] = random.random  # the shadow sampling draw; tests replace it


# ---------------------------------------------------------------- who is measured


def measurement_consent(db: Database, user_id: int) -> bool:
    """Whether the user's latest consents row for search_measurement is granted (P3.1: append-only, the newest
    row for a purpose is the one in force; no row means not granted)."""
    row = db.mongo["consents"].find_one({"user_id": user_id, "purpose": CONSENT_PURPOSE}, {"granted": 1},
                                        sort=[("at", -1), ("_id", -1)])
    return bool(row and row.get("granted"))


def measured(db: Database, user: User | None) -> bool:
    """Whether this caller's searches, clicks and conversions are recorded: a signed-in customer with the
    search-measurement consent. Owners and admins never are, so their consent is never read."""
    return user is not None and user.role == Role.CUSTOMER and measurement_consent(db, user.id)


# ---------------------------------------------------------------- language tags


VOWELS = {"A": "a", "AA": "aa", "I": "i", "II": "ii", "U": "u", "UU": "uu", "E": "e", "EE": "ee", "AI": "ai",
          "O": "o", "OO": "oo", "AU": "au", "VOCALIC R": "ru", "SHORT E": "e", "SHORT O": "o", "CANDRA E": "e",
          "CANDRA O": "o"}
CONSONANTS = {"CA": "ch", "CHA": "chh", "NGA": "n", "NYA": "n", "NNA": "n", "TTA": "t", "TTHA": "th", "DDA": "d",
              "DDHA": "dh", "SSA": "sh", "LLA": "l", "LLLA": "l", "RRA": "r", "VA": "v"}


def romanise(text: str) -> str:
    """A rough Latin spelling of Kannada, Telugu or Devanagari text, from the Unicode character names (consonants
    carry an inherent a unless a vowel sign or virama follows; a final anusvara reads m). Only good enough to tell
    which script term a Latin transliteration is closest to."""
    out: list[str] = []
    pending = False  # a consonant still carrying its inherent vowel
    for i, ch in enumerate(text):
        script, _, kind = unicodedata.name(ch, "").partition(" ")
        if script not in SCRIPT_LANGUAGES:
            if pending:
                out.append("a")
                pending = False
            out.append(ch if ch.isascii() else " ")
            continue
        if kind.startswith("LETTER "):
            sound = kind.removeprefix("LETTER ")
            if pending:
                out.append("a")
            if sound in VOWELS:
                out.append(VOWELS[sound])
                pending = False
            else:
                out.append(CONSONANTS.get(sound, sound.lower()[:-1] if sound.endswith("A") else sound.lower()))
                pending = True
        elif kind.startswith("VOWEL SIGN "):
            out.append(VOWELS.get(kind.removeprefix("VOWEL SIGN "), ""))
            pending = False
        elif kind == "SIGN VIRAMA":
            pending = False
        elif kind in ("SIGN ANUSVARA", "SIGN CANDRABINDU"):
            if pending:
                out.append("a")
                pending = False
            last = i == len(text) - 1 or not text[i + 1].isalpha()
            out.append("m" if last and kind == "SIGN ANUSVARA" else "n")
    if pending:
        out.append("a")
    return "".join(out)


def _fold(text: str) -> str:
    """Spelling variants a transliteration does not settle: doubled letters (long vowels, geminates)."""
    return re.sub(r"(.)\1+", r"\1", text)


def script_language(text: str) -> str | None:
    """kn, te or hi for the first Kannada, Telugu or Devanagari character in text, "other" for the first letter of
    any other non-Latin script, or None when every letter is Latin."""
    for ch in text:
        code = ord(ch)
        for lang, lo, hi in SCRIPT_RANGES:
            if lo <= code <= hi:
                return lang
        if ch.isalpha() and not ch.isascii() and not unicodedata.name(ch, "").startswith("LATIN "):
            return "other"
    return None


def latin_term_language(latin: str, script_terms: dict[str, list[str]]) -> tuple[str, float]:
    """Which language a Latin transliteration in an item's local_terms spells: the language whose script term it is
    closest to, read aloud (romanise). Ties go to Kannada, then Telugu, then Hindi (Ballari's languages, in that
    order): a loanword such as charjar is spelt alike in all three."""
    best, best_score = "kn", -1.0
    for lang in ("kn", "te", "hi"):
        for term in script_terms.get(lang, ()):
            score = fuzz.ratio(_fold(latin), _fold(romanise(term)))
            if score > best_score:
                best, best_score = lang, score
    return best, best_score


@dataclass
class _TermMap:
    terms: dict[str, str]  # Latin local term -> kn-Latn, te-Latn or hi-Latn
    built: float


_term_maps: dict[str, _TermMap] = {}
TERM_MAP_SECONDS = 60 * 60  # catalogue local terms change only through migrations and the seed


def latin_terms(db: Database) -> dict[str, str]:
    """Every Latin-script term in the catalogue's local_terms (P5.2), with the language it transliterates. Built
    per database at most once an hour."""
    key = db.mongo.name
    hit = _term_maps.get(key)
    if hit is not None and time.monotonic() - hit.built < TERM_MAP_SECONDS:
        return hit.terms
    scored: dict[str, tuple[float, int, str]] = {}
    order = {"kn": 0, "te": 1, "hi": 2}
    for item in db.catalog_items.find_raw({"local_terms.0": {"$exists": True}}, {"local_terms": 1}):
        scripts: dict[str, list[str]] = {}
        latin: list[str] = []
        for term in item.get("local_terms") or []:
            lang = script_language(term)
            if lang in order:
                scripts.setdefault(lang, []).append(term)
            elif lang is None:
                latin.append(term)
        for term in latin:
            lang, score = latin_term_language(term, scripts)
            candidate = (score, -order[lang], lang)
            if term not in scored or candidate[:2] > scored[term][:2]:
                scored[term] = candidate
    terms = {term: f"{lang}-Latn" for term, (_, _, lang) in scored.items()}
    _term_maps[key] = _TermMap(terms, time.monotonic())
    return terms


def reset_terms() -> None:
    """Forget the cached Latin term maps (tests)."""
    _term_maps.clear()


def language_tag(db: Database, norm: str, terms: dict[str, str] | None = None) -> str:
    """The language of a normalised query (plan section 8.6). A query with Kannada, Telugu or Devanagari characters
    is kn, te or hi; in a mixed query the first non-Latin script wins. A Latin-script query is kn-Latn, te-Latn or
    hi-Latn when one of its words, word pairs or the whole query is a Latin transliteration in local_terms (the
    longest match wins, then the first), and en otherwise."""
    lang = script_language(norm)
    if lang is not None:
        return lang
    terms = latin_terms(db) if terms is None else terms
    candidates = sorted(_local_term_candidates(norm), key=lambda t: -len(t.split()))
    for candidate in candidates:
        if candidate in terms:
            return terms[candidate]
    return "en"


# ---------------------------------------------------------------- the trail cookie


def _key() -> bytes:
    return hmac.new(settings.secret_key.encode(), b"nearshop-search-trail", hashlib.sha256).digest()


def _epoch(at: datetime) -> int:
    return int(at.replace(tzinfo=UTC).timestamp())


@dataclass
class TrailSearch:
    id: str
    at: int  # epoch seconds
    fp: str  # a keyed hash of the query and filters: which later pages continue this search


@dataclass
class TrailClick:
    search_id: str
    listing: int
    group: str
    position: int
    at: int


@dataclass
class Trail:
    searches: list[TrailSearch] = field(default_factory=list)
    clicks: list[TrailClick] = field(default_factory=list)

    def encode(self) -> str:
        data = {"s": [[s.id, s.at, s.fp] for s in self.searches[-TRAIL_SEARCHES:]],
                "c": [[c.search_id, c.listing, c.group, c.position, c.at] for c in self.clicks[-TRAIL_CLICKS:]]}
        payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        return f"{payload}.{hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]}"

    @classmethod
    def decode(cls, value: str | None) -> Trail | None:
        """The trail in a cookie value, or None when it is missing, malformed or not signed by this server."""
        if not value or "." not in value or len(value) > 4096:
            return None
        payload, _, signature = value.rpartition(".")
        expected = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            searches = [TrailSearch(str(i), int(at), str(fp)) for i, at, fp in data.get("s", [])]
            clicks = [TrailClick(str(s), int(listing), str(g), int(pos), int(at))
                      for s, listing, g, pos, at in data.get("c", [])]
        except (ValueError, TypeError, AttributeError):
            return None
        return cls(searches[-TRAIL_SEARCHES:], clicks[-TRAIL_CLICKS:])


def set_trail(response: Response, trail: Trail) -> None:
    response.set_cookie(TRAIL_COOKIE, trail.encode(), max_age=TRAIL_SECONDS, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/api")


def clear_trail(response: Response) -> None:
    response.delete_cookie(TRAIL_COOKIE, path="/api", httponly=True, samesite="lax", secure=settings.cookie_secure)


def _fingerprint(p: SearchParams, norm: str) -> str:
    """A keyed hash of the query and filters (not reversible without the server's key): a later page continues
    the trail's latest search only when this matches."""
    parts = [norm, p.category, p.sort, p.min_price, p.max_price, p.in_stock_only, p.fulfillment, p.radius_km,
             p.lat, p.lng, p.page_size, p.shop_id]
    return hmac.new(_key(), json.dumps(parts, default=str).encode(), hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------- searches


@dataclass
class SearchPlan:
    """What the search route does for one measured search, decided before run_search runs."""

    search_id: str
    new: bool  # a new search (page 1), or a later page of the trail's latest search
    previous_id: str | None
    trail: Trail
    fingerprint: str
    now: datetime  # the time freshness is judged at, for the live and the shadow rankings alike
    shadow: RankingConfig | None
    cache: dict | None  # run_search's fetch cache, kept for the shadow ranking


def plan(p: SearchParams, cookie: str | None) -> SearchPlan | None:
    """The plan for a measured customer's search, or None when it writes no event: no query, or a later page that
    does not continue the trail's latest search."""
    norm = normalize_query(p.q) if p.q else ""
    if not norm:
        return None
    at = now()
    ts = _epoch(at)
    trail = Trail.decode(cookie) or Trail()
    fresh = [s for s in trail.searches if ts - s.at < TRAIL_SECONDS]
    trail = replace(trail, searches=fresh)
    latest = fresh[-1] if fresh else None
    fp = _fingerprint(p, norm)
    if p.page > 1:
        if latest is None or latest.fp != fp:
            return None
        return SearchPlan(latest.id, False, None, trail, fp, search_now(), None, None)
    previous = latest.id if latest is not None and ts - latest.at < PREVIOUS_SEARCH_SECONDS else None
    candidate = settings.shadow_ranking
    shadow = candidate if candidate is not None and sample() < settings.search_shadow_share else None
    return SearchPlan(secrets.token_hex(12), True, previous, trail, fp, search_now(), shadow,
                      {} if shadow is not None else None)


def stored_query(norm: str) -> tuple[str, bool]:
    """The query text an event may hold, and whether it was redacted: the normalised query only when it is
    QUERY_MAX_CHARS characters or fewer and passes the P5.5 blocklist (no phone number, email, link or abusive
    word)."""
    if len(norm) <= QUERY_MAX_CHARS and suggestion_allowed(norm):
        return norm, False
    return "", True


def _impressions(result: dict, p: SearchParams) -> list[dict]:
    start = (max(p.page, 1) - 1) * p.page_size
    out = []
    for i, g in enumerate(result.get("groups") or []):
        position = start + i + 1
        if position > MAX_IMPRESSIONS:
            break
        out.append({"group": g["key"], "listing": int(g["best_offer"]["id"]), "position": position})
    return out


def _round(value: float) -> float:
    return round(float(value), LOCATION_DECIMALS)


def event_document(sp: SearchPlan, p: SearchParams, result: dict) -> dict:
    norm = normalize_query(p.q)
    query, redacted = stored_query(norm)
    return {
        "_id": sp.search_id,
        "created_at": now(),
        "query": query,
        "redacted": redacted,
        "language": "en",  # set by _write_search, off the request path
        "category": p.category,
        "sort": p.sort,
        "filters": {"min_price": p.min_price, "max_price": p.max_price, "in_stock_only": bool(p.in_stock_only),
                    "fulfillment": p.fulfillment},
        "radius_km": float(p.radius_km),
        "lat": _round(p.lat),
        "lng": _round(p.lng),
        "results_count": int(result.get("total") or 0),
        "corrected": result.get("corrected_from") is not None,
        "ranking": str((result.get("engine") or {}).get("ranking") or settings.ranking.version),
        "impressions": _impressions(result, p),
        "clicks": [],
        "conversions": [],
        "previous_search_id": sp.previous_id,
        "shadow": None,
    }


def record_search(db: Database, sp: SearchPlan, p: SearchParams, result: dict, response: Response) -> None:
    """After a measured search: set the trail cookie and, off the request path, write the event (a new search) or
    add the page's impressions (a later page), then run the shadow ranking when this search was sampled."""
    name = db.mongo.name
    if sp.new:
        doc = event_document(sp, p, result)
        sp.trail.searches.append(TrailSearch(sp.search_id, _epoch(doc["created_at"]), sp.fingerprint))
        engine = dict(result.get("engine") or {})
        db.background(lambda: _write_search(name, doc, sp, p, engine))
    else:
        impressions = _impressions(result, p)
        if impressions:
            db.background(lambda: _add_impressions(name, sp.search_id, impressions))
    set_trail(response, sp.trail)


def _write_search(name: str, doc: dict, sp: SearchPlan, p: SearchParams, engine: dict) -> None:
    db = Database(name)
    doc["language"] = language_tag(db, normalize_query(p.q))
    if doc["category"] is not None and category_by_slug(db, doc["category"]) is None:
        doc["category"] = None  # only a real category slug, never free text from the query string
    db.mongo[COLLECTION].insert_one(doc)
    if sp.shadow is not None:
        shadow = shadow_rank(db, p, sp, engine)
        db.mongo[COLLECTION].update_one({"_id": doc["_id"]}, {"$set": {"shadow": shadow}})


def _add_impressions(name: str, search_id: str, impressions: list[dict]) -> None:
    collection = Database(name).mongo[COLLECTION]
    doc = collection.find_one({"_id": search_id}, {"impressions.position": 1})
    if doc is None:
        return
    shown = {i["position"] for i in doc.get("impressions") or []}
    new = [i for i in impressions if i["position"] not in shown]
    if new:
        collection.update_one({"_id": search_id}, {"$push": {"impressions": {"$each": new}}})


# ---------------------------------------------------------------- shadow ranking


def engine_label(ranking: str, keyword: str, vector: str) -> str:
    return f"{ranking} + {keyword} + {vector}"


def shadow_rank(db: Database, p: SearchParams, sp: SearchPlan, engine: dict) -> dict:
    """Re-rank the search's fetched listings and scores (sp.cache) with the production config and the candidate,
    top SHADOW_TOP groups each, with the time each re-ranking took."""
    assert sp.shadow is not None
    top = replace(p, page=1, page_size=SHADOW_TOP)
    production = settings.ranking

    def ranked(cfg: RankingConfig) -> tuple[dict, float]:
        started = time.perf_counter()
        out = run_search(db, top, log=False, ranking=cfg, cache=sp.cache, now=sp.now)
        return out, (time.perf_counter() - started) * 1000

    prod, prod_ms = ranked(production)
    cand, cand_ms = ranked(sp.shadow)
    keyword = str(engine.get("keyword") or KEYWORD_ENGINE)
    vector = str(engine.get("semantic") or "unknown")
    return {
        "label": engine_label(sp.shadow.version, keyword, vector),
        "ranking": sp.shadow.version,
        "keyword": keyword,
        "vector": vector,
        "production_label": engine_label(production.version, keyword, vector),
        "top": [g["key"] for g in cand["groups"]][:SHADOW_TOP],
        "production_top": [g["key"] for g in prod["groups"]][:SHADOW_TOP],
        "results_count": int(cand["total"]),
        "production_results_count": int(prod["total"]),
        "ms": round(cand_ms, 2),
        "production_ms": round(prod_ms, 2),
    }


# ---------------------------------------------------------------- clicks and conversions


def record_click(db: Database, request: Request, response: Response, user: User | None, listing_id: int,
                 group: str, position: int) -> None:
    """Add a click to the most recent trail search whose impressions hold the group. Nothing without a trail, or
    for a caller who is not measured (whose trail cookie is deleted)."""
    cookie = request.cookies.get(TRAIL_COOKIE)
    if cookie is None:
        return
    if not measured(db, user):
        clear_trail(response)
        return
    trail = Trail.decode(cookie)
    if trail is None:
        return
    at = now()
    ts = _epoch(at)
    ids = [s.id for s in trail.searches if ts - s.at < TRAIL_SECONDS]
    if not ids:
        return
    doc = db.mongo[COLLECTION].find_one({"_id": {"$in": ids}, "impressions.group": group}, {"_id": 1},
                                        sort=[("created_at", -1)])
    if doc is None:
        return
    trail.clicks.append(TrailClick(doc["_id"], listing_id, group, position, ts))
    set_trail(response, trail)
    click = {"group": group, "listing": listing_id, "position": position, "at": at}
    name = db.mongo.name
    db.background(lambda: Database(name).mongo[COLLECTION].update_one(
        {"_id": doc["_id"]}, {"$push": {"clicks": {"$each": [click], "$slice": -MAX_CLICKS}}}))


def attribute(db: Database, request: Request, user: User | None, kind: str, product_ids: Iterable[int]) -> None:
    """After a reservation or order is created: attribute it to the latest trail click within the last 2 hours on
    one of its listings or another listing of the same catalogue item, as a conversion (kind, group and position)
    on that click's search. Off the request path; never fails or changes the create."""
    try:
        cookie = request.cookies.get(TRAIL_COOKIE)
        if cookie is None or user is None or user.role != Role.CUSTOMER:
            return
        trail = Trail.decode(cookie)
        if trail is None or not trail.clicks:
            return
        name, user_id, ids, at = db.mongo.name, user.id, [int(i) for i in product_ids], now()
        db.background(lambda: _attribute(Database(name), user_id, trail, kind, ids, at))
    except Exception as exc:  # attribution never fails a reservation or an order
        log.warning("search attribution skipped: %s", type(exc).__name__)


def _attribute(db: Database, user_id: int, trail: Trail, kind: str, product_ids: list[int], at: datetime) -> None:
    if not product_ids or not measurement_consent(db, user_id):
        return
    rows = db.products.find_raw({"_id": {"$in": product_ids}}, {"catalog_item_id": 1})
    listings = set(product_ids)
    groups = {f"c{r['catalog_item_id']}" if r.get("catalog_item_id") else f"p{r['_id']}" for r in rows}
    ts = _epoch(at)
    # The latest click wins: the trail keeps clicks in the order they came, and clicks in the same second tie.
    for _, click in sorted(enumerate(trail.clicks), key=lambda pair: (pair[1].at, pair[0]), reverse=True):
        if not 0 <= ts - click.at <= TRAIL_SECONDS:
            continue
        if click.listing in listings or click.group in groups:
            conversion = {"kind": kind, "group": click.group, "position": click.position, "at": at}
            db.mongo[COLLECTION].update_one({"_id": click.search_id},
                                            {"$push": {"conversions": {"$each": [conversion],
                                                                       "$slice": -MAX_CONVERSIONS}}})
            return


# ---------------------------------------------------------------- online metrics (plan section 8.6)


def _rate(hits: int, n: int) -> float | None:
    return round(hits / n, 4) if n else None


def _figure(hits: int, n: int) -> dict:
    return {"value": _rate(hits, n), "n": n}


def _first_click(e: dict) -> dict | None:
    clicks = e.get("clicks") or []
    return min(clicks, key=lambda c: c["at"]) if clicks else None


def _reformulated(e: dict, after: dict | None) -> bool:
    """Followed within 2 minutes, in the same trail, by a different query with no click in between."""
    if after is None or after["created_at"] - e["created_at"] > timedelta(seconds=REFORMULATION_SECONDS):
        return False
    if any(c["at"] < after["created_at"] for c in e.get("clicks") or []):
        return False
    if e.get("redacted") and after.get("redacted"):
        return False  # two redacted queries cannot be compared
    return (e.get("query") or "", bool(e.get("redacted"))) != (after.get("query") or "", bool(after.get("redacted")))


def event_figures(events: list[dict], following: dict[str, dict]) -> dict:
    """The consented-search metrics over events (each a search with a query). following maps a search id to the
    next search of its trail."""
    n = len(events)
    success = zero = reformulated = converted = 0
    first_positions: list[int] = []
    for e in events:
        conversions = e.get("conversions") or []
        if e.get("clicks") or any(c["at"] - e["created_at"] <= timedelta(seconds=SUCCESS_SECONDS)
                                  for c in conversions):
            success += 1
        if not e.get("results_count"):
            zero += 1
        if _reformulated(e, following.get(e["_id"])):
            reformulated += 1
        if conversions:
            converted += 1
        if (first := _first_click(e)) is not None:
            first_positions.append(int(first["position"]))
    clicks = len(first_positions)
    return {
        "success_rate": _figure(success, n),
        "zero_result_rate": _figure(zero, n),
        "reformulation_rate": _figure(reformulated, n),
        "click_position": {
            "mean": round(statistics.fmean(first_positions), 2) if clicks else None,
            "median": statistics.median(first_positions) if clicks else None,
            "top3_share": _rate(sum(1 for pos in first_positions if pos <= 3), clicks),
            "n": clicks,
        },
        "conversion_rate": _figure(converted, n),
    }


def _empty_figures(n: int) -> dict:
    return {"success_rate": {"value": None, "n": n}, "zero_result_rate": {"value": None, "n": n},
            "reformulation_rate": {"value": None, "n": n},
            "click_position": {"mean": None, "median": None, "top3_share": None, "n": 0},
            "conversion_rate": {"value": None, "n": n}}


def _history_counts(db: Database, since: datetime, recent: datetime) -> list[dict]:
    """search_history in the window, per normalised query: rows and zero-result rows, overall and since recent."""
    return db.search_history.aggregate([
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {
            "_id": "$normalized_query",
            "n": {"$sum": 1},
            "zero": {"$sum": {"$cond": [{"$eq": ["$results_count", 0]}, 1, 0]}},
            "n_recent": {"$sum": {"$cond": [{"$gte": ["$created_at", recent]}, 1, 0]}},
            "zero_recent": {"$sum": {"$cond": [{"$and": [{"$gte": ["$created_at", recent]},
                                                         {"$eq": ["$results_count", 0]}]}, 1, 0]}},
        }},
    ])


def online_metrics(db: Database, at: datetime | None = None) -> dict:
    """The online metrics of plan section 8.6 over the last 7 and 30 days, overall and per language. Each figure
    carries its sample size; a language with fewer than MIN_LANGUAGE_SEARCHES searches in the window has
    enough_data false and no figures. The zero-result rate is given over all of search_history (the plan section
    8.23 definition, check 8.3; its rows are tagged with a language when read) and over consented searches."""
    at = at or now()
    longest = max(WINDOWS_DAYS)
    since = at - timedelta(days=longest)
    projection = {"impressions": 0, "shadow": 0, "category": 0, "filters": 0}
    events = list(db.mongo[COLLECTION].find({"created_at": {"$gte": since}}, projection))
    following: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: e["created_at"]):
        prev = e.get("previous_search_id")
        if prev and prev not in following:
            following[prev] = e
    terms = latin_terms(db)
    tags: dict[str, str] = {}

    def tag(query: str) -> str:
        if query not in tags:
            tags[query] = language_tag(db, query, terms) if query else "en"
        return tags[query]

    history = []
    for row in _history_counts(db, since, at - timedelta(days=min(WINDOWS_DAYS))):
        history.append((tag(row["_id"] if isinstance(row["_id"], str) else ""), row))
    windows = []
    for days in WINDOWS_DAYS:
        start = at - timedelta(days=days)
        in_window = [e for e in events if e["created_at"] >= start]
        recent = days == min(WINDOWS_DAYS) and days != longest
        hist_n: dict[str, int] = {}
        hist_zero: dict[str, int] = {}
        for lang, row in history:
            n, zero = (row["n_recent"], row["zero_recent"]) if recent else (row["n"], row["zero"])
            hist_n[lang] = hist_n.get(lang, 0) + n
            hist_zero[lang] = hist_zero.get(lang, 0) + zero
        by_lang: dict[str, list[dict]] = {}
        for e in in_window:
            by_lang.setdefault(e.get("language") or "en", []).append(e)
        total_hist = sum(hist_n.values())
        rows = [{"language": "all", "enough_data": True, "searches": len(in_window), "history_searches": total_hist,
                 "zero_result_rate_all": _figure(sum(hist_zero.values()), total_hist),
                 **event_figures(in_window, following)}]
        order = {lang: i for i, lang in enumerate(LANGUAGES)}
        for lang in sorted(set(by_lang) | {k for k, v in hist_n.items() if v},
                           key=lambda x: (order.get(x, len(order)), x)):
            group = by_lang.get(lang, [])
            n_hist = hist_n.get(lang, 0)
            enough = len(group) >= MIN_LANGUAGE_SEARCHES
            rows.append({
                "language": lang, "enough_data": enough, "searches": len(group), "history_searches": n_hist,
                "history_enough_data": n_hist >= MIN_LANGUAGE_SEARCHES,
                "zero_result_rate_all": (_figure(hist_zero.get(lang, 0), n_hist) if n_hist >= MIN_LANGUAGE_SEARCHES
                                         else {"value": None, "n": n_hist}),
                **(event_figures(group, following) if enough else _empty_figures(len(group))),
            })
        windows.append({"days": days, "since": iso(start), "rows": rows})
    return {"at": iso(at), "ranking": settings.ranking.version, "min_language_searches": MIN_LANGUAGE_SEARCHES,
            "windows": windows}


# ---------------------------------------------------------------- shadow comparison (plan section 8.6)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered))) - 1))], 2)


def _overlap(a: list[str], b: list[str]) -> float:
    a, b = a[:OVERLAP_TOP], b[:OVERLAP_TOP]
    if not a and not b:
        return 1.0
    return len(set(a) & set(b)) / max(len(a), len(b))


def shadow_comparison(db: Database) -> list[dict]:
    """Per shadow label: shadowed searches and days covered, the mean overlap of the two top 10s, the difference
    in zero-result searches, for shadowed searches with a click how often the clicked group is in the candidate's
    top 10 and its mean change in rank, the candidate's time p95, and the plan section 8.6 guardrails."""
    rows = db.mongo[COLLECTION].find({"shadow.label": {"$type": "string"}},
                                     {"created_at": 1, "clicks": 1, "results_count": 1, "shadow": 1})
    by_label: dict[str, list[dict]] = {}
    for e in rows:
        by_label.setdefault(e["shadow"]["label"], []).append(e)
    out = []
    for label, events in sorted(by_label.items()):
        n = len(events)
        first, last = min(e["created_at"] for e in events), max(e["created_at"] for e in events)
        days = round((last - first).total_seconds() / 86400, 2)
        overlap = round(statistics.fmean(_overlap(e["shadow"]["production_top"], e["shadow"]["top"])
                                         for e in events), 4)
        prod_zero = sum(1 for e in events if not e["shadow"]["production_results_count"])
        cand_zero = sum(1 for e in events if not e["shadow"]["results_count"])
        zero_pp = round((cand_zero - prod_zero) / n * 100, 2)
        clicked = in_top10 = 0
        rank_changes: list[int] = []
        for e in events:
            if (click := _first_click(e)) is None:
                continue
            clicked += 1
            cand_top, prod_top = e["shadow"]["top"], e["shadow"]["production_top"]
            if click["group"] in cand_top[:OVERLAP_TOP]:
                in_top10 += 1
            if click["group"] in cand_top and click["group"] in prod_top:
                rank_changes.append(cand_top.index(click["group"]) - prod_top.index(click["group"]))
        share = _rate(in_top10, clicked)
        cand_p95 = _p95([e["shadow"]["ms"] for e in events])
        prod_p95 = _p95([e["shadow"]["production_ms"] for e in events])
        sample_shadow = events[-1]["shadow"]
        production_engines = sample_shadow["production_label"].split(" + ")[1:]
        engine_candidate = [sample_shadow["keyword"], sample_shadow["vector"]] != production_engines
        guardrails = [
            {"name": f"at least {GUARDRAIL_MIN_DAYS} days and {GUARDRAIL_MIN_SEARCHES} shadowed searches",
             "pass": days >= GUARDRAIL_MIN_DAYS and n >= GUARDRAIL_MIN_SEARCHES},
            {"name": f"no results for at most {GUARDRAIL_MAX_ZERO_PP} percentage points more searches",
             "pass": zero_pp <= GUARDRAIL_MAX_ZERO_PP},
            {"name": f"the clicked result in the candidate's top 10 in at least "
                     f"{round(GUARDRAIL_MIN_CLICKED_IN_TOP10 * 100)}% of searches with a click",
             "pass": share is not None and share >= GUARDRAIL_MIN_CLICKED_IN_TOP10},
            {"name": "an engine candidate's time p95 no higher than production's",
             "pass": (cand_p95 is not None and prod_p95 is not None and cand_p95 <= prod_p95)
             if engine_candidate else None},
        ]
        out.append({
            "label": label, "ranking": sample_shadow["ranking"], "keyword": sample_shadow["keyword"],
            "vector": sample_shadow["vector"], "engine_candidate": engine_candidate,
            "searches": n, "first_at": iso(first), "last_at": iso(last), "days_covered": days,
            "mean_overlap_top10": overlap,
            "zero_result": {"production": prod_zero, "candidate": cand_zero, "difference": cand_zero - prod_zero,
                            "difference_pp": zero_pp},
            "clicked": {"searches": clicked, "in_candidate_top10": in_top10, "in_candidate_top10_share": share,
                        "mean_rank_change": round(statistics.fmean(rank_changes), 2) if rank_changes else None},
            "time_p95_ms": {"candidate": cand_p95, "production": prod_p95},
            "guardrails": guardrails,
            "passed": all(g["pass"] for g in guardrails if g["pass"] is not None),
        })
    return out


def online_report(db: Database) -> dict:
    """What the admin view shows: the online metrics and the shadow comparison."""
    return {**online_metrics(db), "shadow": shadow_comparison(db),
            "shadow_settings": {"candidate": settings.search_shadow_ranking_version or None,
                                "share": settings.search_shadow_share}}


def save_snapshot(db: Database) -> dict:
    """Store the admin view's figures as a model_runs entry named search_online: aggregates per language only, with
    the windows and the live ranking config version, never query text (plan section 8.4)."""
    started = time.perf_counter()
    report = online_report(db)
    largest = next((w for w in report["windows"] if w["days"] == max(WINDOWS_DAYS)), report["windows"][-1])
    run = db.model_runs.insert(ModelRun(
        name=SNAPSHOT_NAME, algorithm="online search metrics (plan section 8.6)",
        n_samples=int(largest["rows"][0]["searches"]),
        metrics={"windows": report["windows"], "shadow": report["shadow"], "ranking": report["ranking"],
                 "window_days": list(WINDOWS_DAYS)},
        data_note=f"Live ranking config {report['ranking']}; windows of {' and '.join(map(str, WINDOWS_DAYS))} days.",
        duration_ms=int((time.perf_counter() - started) * 1000)))
    return {"id": run.id, "at": iso(run.created_at), "ranking": report["ranking"]}


def snapshot_rows(db: Database, limit: int = 10) -> list[dict[str, Any]]:
    """The latest saved snapshots, newest first."""
    return [{"id": r.id, "at": iso(r.created_at), "ranking": r.metrics.get("ranking")}
            for r in db.model_runs.find({"name": SNAPSHOT_NAME}, sort=[("created_at", -1)], limit=limit)]
