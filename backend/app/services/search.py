"""NearShop search pipeline.

    query -> normalise + synonyms -> keyword (MongoDB text index) + semantic (sentence embeddings)
          -> location filter (2dsphere index + Haversine) -> availability / price / fulfilment filters
          -> hybrid relevance + proximity ranking -> group listings of the same item -> results

The keyword and semantic stages each run on the engine settings choose (P5.7; app/ai/atlas_search.py): the text
index or Atlas Search, and the in-process vector index or Atlas Vector Search. The defaults are today's engines;
fusion, grouping, filters and the ranking config are NearShop's own with every engine.

Results are grouped by catalog item so a customer sees "Samsung 25W charger: 4 shops nearby
from Rs 1,249" and can compare shops, instead of four near-identical cards.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import lru_cache

from rapidfuzz import fuzz, process

from app.ai import atlas_search
from app.ai.semantic_index import OVERLAP, index, nightly_slot
from app.core import cache_version
from app.core.config import SEARCH_SIMILARITY_SCALE, EnginePair, RankingConfig, settings
from app.core.database import Database, utcnow
from app.models import Product, SearchEvent, Shop
from app.schemas.serializers import STALE_DAYS, freshness, listing, shop_brief
from app.services.loaders import categories, category_by_slug, with_categories, with_category
from app.utils.geo import haversine_km, within_km
from app.utils.text import expand_synonyms, is_word, normalize_query, search_tokens, word_parts

# Every ranking constant and group weight lives in a versioned RankingConfig (app/core/config.py, P5.5): the
# version NEARSHOP_SEARCH_RANKING_VERSION names, r1 by default (the hand-set values used before P5.5). An exact
# match on a listing's local_terms (Kannada, Telugu, Hindi or a transliteration; P5.2) adds w_local_term, the
# text index's brand weight, to its keyword score once per matched term: it counts as much as a brand match.
# W_LOCAL_TERM is the active config's value, read once at import, for code that checks it against the text
# index's brand weight (database/schema.py); ranking reads the config itself.
W_LOCAL_TERM = settings.ranking.w_local_term
# The similarity scale the semantic index returns (config.SEARCH_SIMILARITY_SCALE, the all-MiniLM-L6-v2
# calibration); r1's sem_floor and sem_ceil equal it, and a tuned config maps from this scale with its own.
SEM_FLOOR, SEM_CEIL = SEARCH_SIMILARITY_SCALE.sem_floor, SEARCH_SIMILARITY_SCALE.sem_ceil
# Search results never show these, so they are not fetched.
LISTING_FIELDS = {"description": 0, "specs": 0}
# What run_search reports as engine.keyword, per keyword engine (P5.7); the P5.6 shadow labels use the same names.
KEYWORD_ENGINE_LABELS = {"text": "mongodb-text-index", "atlas_search": "atlas-search"}
ATLAS_VECTOR_LABEL = "atlas-vector-search"

# Stock freshness (P7.4; bands and multipliers in app/schemas/serializers.py): a group's score is multiplied by
# the freshness multiplier of its freshest offer, and the in-stock-only filter leaves out listings whose stock was
# last confirmed more than STALE_DAYS ago. Freshness is judged at search_now(): the current time, unless the
# evaluation harness fixed the clock to its snapshot's seeding time (set_clock), so a reused snapshot's listings
# do not go stale as it ages.
_clock: datetime | None = None


def set_clock(at: datetime | None) -> None:
    """Fix the clock freshness is judged at (None: the current time again). For the evaluation harness only."""
    global _clock
    _clock = at


def search_now() -> datetime:
    return _clock or utcnow()


@dataclass
class SearchParams:
    q: str = ""
    lat: float = 0.0
    lng: float = 0.0
    radius_km: float = 5.0
    category: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    in_stock_only: bool = False
    fulfillment: str | None = None  # pickup | delivery
    sort: str = "relevance"  # relevance | distance | price_asc | price_desc
    shop_id: int | None = None
    page: int = 1
    page_size: int = 20


def nearby_shops(db: Database, lat: float, lng: float, radius_km: float, fulfillment: str | None = None,
                 shop_id: int | None = None) -> dict[int, tuple[Shop, float]]:
    query: dict = {"is_active": True, "location": within_km(lat, lng, radius_km)}
    if shop_id:
        query["_id"] = shop_id
    out = {}
    for s in with_categories(db, db.shops.find(query)):
        d = haversine_km(lat, lng, s.lat, s.lng)
        if d > radius_km:
            continue
        if fulfillment == "delivery" and not (s.offers_delivery and d <= s.delivery_radius_km):
            continue
        if fulfillment == "pickup" and not s.offers_pickup:
            continue
        out[s.id] = (s, d)
    return out


def _completions(db: Database, tokens: list[str]) -> list[str]:
    """Prefix-match the word being typed, so "chargi" still finds "charger": the text index matches whole (stemmed)
    words, so the partial word is completed from the catalogue vocabulary."""
    last = tokens[-1] if tokens else ""
    if len(last) < 3:
        return []
    return sorted(w for w in _vocabulary(db) if w.startswith(last) and w != last)[:8]


def keyword_scores(db: Database, terms: list[str]) -> dict[int, float]:
    """Text-index relevance over name/brand/keywords/description (weights 10/6/4/1). Higher = better."""
    tokens = search_tokens(terms)
    if not tokens:
        return {}
    tokens += _completions(db, tokens)
    rows = db.products.find_raw({"$text": {"$search": " ".join(dict.fromkeys(tokens))}},
                                {"score": {"$meta": "textScore"}})
    return {r["_id"]: r["score"] for r in rows}


def atlas_keyword_scores(db: Database, norm: str, shop_ids: list[int]) -> dict[int, float]:
    """Atlas Search relevance (P5.7) over the same fields and weights, for the listings in shop_ids, on the text
    index's scale. Synonyms come from the index's synonym mapping (the search_synonyms source), not from the
    expanded terms; the word being typed is completed as for the text index."""
    words = search_tokens(norm.split())
    if not words:
        return {}
    return atlas_search.keyword_scores(db, words, _completions(db, words), shop_ids)


def _local_term_candidates(norm: str) -> list[str]:
    """What a query can match in local_terms, exactly: each word, each pair of adjacent words, and the
    whole query (local terms may be phrases, such as the Kannada for "bike oil"). A hyphenated query
    is also tried with spaces, as people join or split the two words of a name either way."""
    out: list[str] = []
    for text in dict.fromkeys([norm, norm.replace("-", " ")]):
        words = text.split()
        out += [*words, *(f"{a} {b}" for a, b in zip(words, words[1:], strict=False)), " ".join(words)]
    return list(dict.fromkeys(w for w in out if w))


def local_term_scores(db: Database, norm: str, w_local_term: float | None = None) -> dict[int, float]:
    """Listings whose local_terms contain a word, adjacent pair or the whole of the normalised query, scored
    w_local_term (the ranking config's, by default) per matched term. Listings created from the catalogue after
    migration 4 copied the terms are matched through their catalog item's own local_terms."""
    candidates = _local_term_candidates(norm)
    if not norm or not candidates:
        return {}
    weight = settings.ranking.w_local_term if w_local_term is None else w_local_term
    wanted = set(candidates)
    by_item = {c["_id"]: len(wanted.intersection(c["local_terms"]))
               for c in db.catalog_items.find_raw({"local_terms": {"$in": candidates}}, {"local_terms": 1})}
    clauses: list[dict] = [{"local_terms": {"$in": candidates}}]
    if by_item:
        clauses.append({"catalog_item_id": {"$in": list(by_item)}})
    out: dict[int, float] = {}
    for r in db.products.find_raw({"$or": clauses}, {"local_terms": 1, "catalog_item_id": 1}):
        own = len(wanted.intersection(r.get("local_terms") or []))
        matched = max(own, by_item.get(r.get("catalog_item_id"), 0))
        if matched:
            out[r["_id"]] = weight * matched
    return out


def _keyword_stage(db: Database, norm: str, expanded: list[str], engine: str = "text",
                   shop_ids: list[int] | None = None) -> tuple[dict[int, float], dict[int, int]]:
    """Keyword scores from the chosen engine, and the number of local terms each listing matched (weighted later,
    by the ranking config). Atlas Search is asked only about the listings of shop_ids."""
    if engine == "atlas_search":
        kw = atlas_keyword_scores(db, norm, shop_ids or [])
    else:
        kw = keyword_scores(db, norm.split() + expanded)
    return kw, {pid: round(bonus) for pid, bonus in local_term_scores(db, norm, 1.0).items()}


def _vector_stage(db: Database, norm: str, engine: str, shop_ids: list[int]) -> dict[int, float]:
    if engine == "atlas_vector":
        return atlas_search.vector_scores(db, norm, shop_ids)
    return index.query(db, norm)


def engine_labels(engines: EnginePair) -> tuple[str, str]:
    """engine.keyword and engine.semantic as a search with these engines reports them."""
    keyword = KEYWORD_ENGINE_LABELS[engines.keyword]
    if engines.vector == "atlas_vector":
        from app.ai.semantic_index import get_provider

        return keyword, f"{ATLAS_VECTOR_LABEL}:{get_provider().name}"
    return keyword, index.provider_name or "loading"


def _norm_sem(sim: float, cfg: RankingConfig) -> float:
    return min(max((sim - cfg.sem_floor) / (cfg.sem_ceil - cfg.sem_floor), 0.0), 1.0)


def _candidates(db: Database, p: SearchParams, norm: str, expanded: list[str], now: datetime,
                engines: EnginePair | None = None) -> tuple:
    """Everything run_search fetches, none of it touched by the ranking config: the shops in radius, the
    listings that pass the filters, keyword scores, local-term match counts and semantic similarities, the last
    two from the chosen engines (settings.search_engines by default)."""
    engines = engines or settings.search_engines
    category = category_by_slug(db, p.category)
    shops = nearby_shops(db, p.lat, p.lng, p.radius_km, p.fulfillment, p.shop_id)

    query: dict = {"is_active": True, "shop_id": {"$in": list(shops)}}
    if p.category:
        query["category_id"] = category.id if category else -1
    if p.min_price is not None or p.max_price is not None:
        query["price"] = {**({"$gte": p.min_price} if p.min_price is not None else {}),
                          **({"$lte": p.max_price} if p.max_price is not None else {})}
    if p.in_stock_only:  # in stock, and confirmed within STALE_DAYS (P7.4)
        query["quantity"] = {"$gt": 0}
        query["stock_updated_at"] = {"$gte": now - timedelta(days=STALE_DAYS)}
    # Listings, keyword scores and semantic scores are independent: fetch them side by side.
    shop_ids = list(shops)
    products, (kw, local), sem = db.gather(
        lambda: with_category(db, db.products.find(query, projection=LISTING_FIELDS)) if shops else [],
        lambda: _keyword_stage(db, norm, expanded, engines.keyword, shop_ids) if norm else ({}, {}),
        lambda: _vector_stage(db, norm, engines.vector, shop_ids) if norm else {},
    )
    return shops, products, kw, local, sem


def run_search(db: Database, p: SearchParams, user_id: int | None = None, log: bool = True,
               ranking: RankingConfig | None = None, cache: dict | None = None, now: datetime | None = None,
               engines: EnginePair | None = None) -> dict:
    """Search. `ranking` overrides the configured ranking config (the evaluation and tuning harnesses).
    `cache`, a dict kept by the caller across calls, memoises the fetch stage per query and filters, so a
    tuning run can re-rank the same candidates under many configs. The live API passes one only for a search it
    shadows (P5.6, app/services/search_events.py), together with `now`, the time freshness is judged at
    (search_now() by default), so the shadow ranking re-ranks exactly the listings and scores the customer's
    search fetched. `engines` overrides the configured engines (P5.7: the evaluation harness and the shadow engine
    pair); the cache keeps each engine pair's fetch apart."""
    cfg = ranking or settings.ranking
    engines = engines or settings.search_engines
    norm = normalize_query(p.q) if p.q else ""
    expanded = expand_synonyms(norm) if norm else []
    now = now or search_now()
    if cache is None:
        shops, products, kw, local, sem = _candidates(db, p, norm, expanded, now, engines)
    else:
        key = (norm, p.lat, p.lng, p.radius_km, p.category, p.min_price, p.max_price, p.in_stock_only,
               p.fulfillment, p.shop_id, now if p.in_stock_only else None, engines)
        if key not in cache:
            cache[key] = _candidates(db, p, norm, expanded, now, engines)
        shops, products, kw, local, sem = cache[key]
    if local:
        kw = dict(kw)
        for pid, matched in local.items():
            kw[pid] = kw.get(pid, 0.0) + cfg.w_local_term * matched

    relevance: dict[int, float] = {}
    if norm:
        kw_max = max((kw.get(pr.id, 0.0) for pr in products), default=0.0) or 1.0
        for pr in products:
            k = kw.get(pr.id, 0.0) / kw_max
            s = sem.get(pr.id, 0.0)
            if k <= 0 and s < cfg.sem_min_match:
                continue
            relevance[pr.id] = cfg.w_semantic * _norm_sem(s, cfg) + cfg.w_keyword * k
        if relevance:
            best = max(relevance.values())
            relevance = {k: v for k, v in relevance.items() if v >= best * cfg.relative_cutoff}
        products = [pr for pr in products if pr.id in relevance]
    else:
        relevance = {pr.id: 1.0 for pr in products}

    shop_cache: dict[int, dict] = {}

    def shop_json(sid: int) -> dict:
        if sid not in shop_cache:
            s, d = shops[sid]
            shop_cache[sid] = shop_brief(s, d)
        return shop_cache[sid]

    # Group listings of the same catalog item.
    groups: dict[str, list[Product]] = defaultdict(list)
    for pr in products:
        groups[f"c{pr.catalog_item_id}" if pr.catalog_item_id else f"p{pr.id}"].append(pr)

    group_rows = []
    category_counts: dict[str, int] = defaultdict(int)
    for key, items in groups.items():
        in_stock = [x for x in items if x.quantity > 0]
        pool = in_stock or items
        nearest = min(shops[x.shop_id][1] for x in pool)
        rel = max(relevance[x.id] for x in items)
        proximity = max(0.0, 1 - nearest / max(p.radius_km, 0.5))
        score = cfg.w_relevance * rel + cfg.w_proximity * proximity + (cfg.w_in_stock if in_stock else 0.0)
        if not in_stock:
            score *= cfg.out_of_stock_factor
        score *= max(freshness(x.stock_updated_at, now)["multiplier"] for x in pool)  # P7.4
        prices = [x.price for x in pool]
        # Best offer: in stock, balancing price and distance.
        lo, hi = min(prices), max(prices)
        best = min(
            pool,
            key=lambda x: ((x.price - lo) / (hi - lo) if hi > lo else 0) + shops[x.shop_id][1] / max(p.radius_km, 1),
        )
        offers = sorted(items, key=lambda x: (x.quantity <= 0, shops[x.shop_id][1]))
        head = best
        category_counts[head.category.slug] += 1
        pictured = next((listing(x, now=now) for x in items if x.image_path), None)  # the group's photo, any offer's
        group_rows.append(
            {
                "key": key,
                "catalog_item_id": head.catalog_item_id,
                "name": head.name,
                "brand": head.brand,
                "unit": head.unit,
                "icon": head.icon,
                "image_url": pictured["image_url"] if pictured else None,
                "thumbnail_url": pictured["thumbnail_url"] if pictured else None,
                "category": head.category.slug,
                "min_price": min(x.price for x in pool),
                "max_price": max(x.price for x in pool),
                "mrp": head.mrp,
                "nearest_km": round(nearest, 2),
                "shop_count": len(items),
                "in_stock_count": len(in_stock),
                "relevance": round(rel, 3),
                "score": round(score, 4),
                "best_offer": listing(best, shop_json(best.shop_id), now),
                "offers": [listing(x, shop_json(x.shop_id), now) for x in offers[:6]],
            }
        )

    sorters = {
        "relevance": lambda g: -g["score"],
        "distance": lambda g: (g["in_stock_count"] == 0, g["nearest_km"]),
        "price_asc": lambda g: (g["in_stock_count"] == 0, g["min_price"]),
        "price_desc": lambda g: (g["in_stock_count"] == 0, -g["min_price"]),
    }
    if not norm and p.sort == "relevance":
        # Browsing without a query: in-stock items near you first.
        group_rows.sort(key=lambda g: (g["in_stock_count"] == 0, g["nearest_km"], -g["shop_count"]))
    else:
        group_rows.sort(key=sorters.get(p.sort, sorters["relevance"]))

    total = len(group_rows)
    start = (max(p.page, 1) - 1) * p.page_size
    page_rows = group_rows[start : start + p.page_size]

    # Map pins: every shop with at least one matching listing.
    pins: dict[int, dict] = {}
    for pr in products:
        pin = pins.setdefault(pr.shop_id, {**shop_json(pr.shop_id), "match_count": 0, "in_stock_count": 0,
                                           "min_price": None})
        pin["match_count"] += 1
        if pr.quantity > 0:
            pin["in_stock_count"] += 1
            pin["min_price"] = pr.price if pin["min_price"] is None else min(pin["min_price"], pr.price)

    did_you_mean = suggest_correction(db, norm) if norm and total < 3 else None

    # Nothing found but the query looks misspelled: show results for the correction instead of an empty page.
    if total == 0 and did_you_mean:
        corrected = run_search(db, replace(p, q=did_you_mean), user_id=user_id, log=log, ranking=ranking,
                               cache=cache, now=now, engines=engines)
        if corrected["total"]:
            return {**corrected, "query": p.q, "corrected_from": p.q, "did_you_mean": None}

    if log and norm:
        # Logged after the response is on its way: analytics must not make search slower.
        event = SearchEvent(user_id=user_id, query=p.q[:200], normalized_query=norm[:200], category_slug=p.category,
                            results_count=total, lat=round(p.lat, 3), lng=round(p.lng, 3))
        db.background(lambda: Database(db.mongo.name).search_history.insert(event))

    keyword_label, semantic_label = engine_labels(engines)
    return {
        "query": p.q,
        "normalized_query": norm,
        "expanded_terms": expanded,
        "did_you_mean": did_you_mean,
        "corrected_from": None,
        "total": total,
        "page": p.page,
        "page_size": p.page_size,
        "has_more": start + p.page_size < total,
        "shops_in_radius": len(shops),
        "groups": page_rows,
        "shops": sorted(pins.values(), key=lambda s: s["distance_km"]),
        "category_counts": dict(category_counts),
        "engine": {"semantic": semantic_label, "keyword": keyword_label, "ranking": cfg.version},
    }


# ---------------------------------------------------------------- "did you mean"

# Per database: the words, and the shared listings version they were built for (app/core/cache_version.py,
# P8.1), so every process's corrections follow the listings within 5 seconds. The catalogue and category
# words are read in full only when the vocabulary is first built, after a version that went down (a reset
# database) and once a night: the app never writes catalogue items or categories (seeds and migrations do).
# A new listings version re-reads only the listings changed since the last refresh (by updated_at, as the
# semantic index does), so a bump that changed no listing (an admin verifying a shop) reads no words at all.
@dataclass(frozen=True)
class _Vocab:
    version: int
    base: frozenset[str]  # catalogue names, brands, tags and local terms, and category names
    listings: dict[int, frozenset[str]]  # active shop-specific listings (no catalogue item): id -> words
    words: frozenset[str]  # base and every listing's words
    since: datetime  # the next refresh re-reads listings with updated_at at or after this
    full_at: datetime  # when the catalogue and every listing were last read


_vocab: dict[str, _Vocab] = {}
# Vocabulary words: Latin letters (lower-cased), or Kannada, Telugu or Devanagari letters with their vowel
# signs and viramas (P5.2). Three characters or more, as before.
_VOCAB_WORD = re.compile(r"[a-z]{3,}|[\u0900-\u097f\u0c00-\u0c7f\u0c80-\u0cff]{3,}")
_LISTING_FIELDS = {"name": 1, "keywords": 1, "catalog_item_id": 1, "is_active": 1}
# Full builds and incremental refreshes of the vocabulary, for tests.
vocab_builds = {"full": 0, "refresh": 0}


def _vocab_words(text: str) -> list[str]:
    return [w for part in word_parts(text.lower()) for w in _VOCAB_WORD.findall(part)]


def _listing_words(row: dict) -> frozenset[str] | None:
    """A listing's vocabulary words, or None when it adds none (inactive, or a catalogue item's listing)."""
    if not row.get("is_active") or row.get("catalog_item_id") is not None:
        return None
    return frozenset(_vocab_words(f"{row['name']} {row.get('keywords') or ''}"))


def _union(base: frozenset[str], listings: dict[int, frozenset[str]]) -> frozenset[str]:
    return base.union(*listings.values())


def _full_vocabulary(db: Database, version: int) -> _Vocab:
    started = utcnow()
    base: set[str] = set()
    for c in db.catalog_items.find_raw({}, {"name": 1, "brand": 1, "tags": 1, "local_terms": 1}):
        for chunk in [c["name"], c.get("brand") or "", *(c.get("tags") or []), *(c.get("local_terms") or [])]:
            base.update(_vocab_words(chunk))
    for c in categories(db).values():
        base.update(_vocab_words(c.name))
    listings = {r["_id"]: w for r in db.products.find_raw({"catalog_item_id": None, "is_active": True}, _LISTING_FIELDS)
                if (w := _listing_words(r)) is not None}
    vocab_builds["full"] += 1
    frozen = frozenset(base)
    return _Vocab(version, frozen, listings, _union(frozen, listings), started - OVERLAP, started)


def _refreshed_vocabulary(db: Database, old: _Vocab, version: int) -> _Vocab:
    started = utcnow()
    rows = db.products.find_raw({"updated_at": {"$gte": old.since}}, _LISTING_FIELDS)
    vocab_builds["refresh"] += 1
    if not rows:
        return replace(old, version=version, since=started - OVERLAP)
    listings = dict(old.listings)
    for r in rows:
        words = _listing_words(r)
        if words is None:
            listings.pop(r["_id"], None)
        else:
            listings[r["_id"]] = words
    return replace(old, version=version, listings=listings, words=_union(old.base, listings),
                   since=started - OVERLAP)


def _vocabulary(db: Database) -> frozenset[str]:
    """Every word customers could mean: catalogue names, brands, tags, category names and the
    names of shop-specific listings, and the catalogue's local terms (Kannada, Telugu, Devanagari and Latin
    transliterations). Cached per process, and refreshed when the shared listings version changes."""
    key = db.mongo.name
    version = cache_version.watcher.version(db.mongo, cache_version.LISTINGS)  # read before the words
    hit = _vocab.get(key)
    if hit is None or version < hit.version or hit.full_at < nightly_slot(utcnow()):
        hit = _vocab[key] = _full_vocabulary(db, version)
    elif hit.version != version:
        hit = _vocab[key] = _refreshed_vocabulary(db, hit, version)
    return hit.words


def suggest_correction(db: Database, norm: str) -> str | None:
    vocab = _vocabulary(db)
    if not vocab:
        return None
    changed = False
    out = []
    for word in norm.split():
        if len(word) < 4 or word in vocab or not is_word(word):
            out.append(word)
            continue
        match = process.extractOne(word, vocab, scorer=fuzz.ratio, score_cutoff=78)
        if match:
            out.append(match[0])
            changed = True
        else:
            out.append(word)
    return " ".join(out) if changed else None


# ---------------------------------------------------------------- suggestions

# Suggestion hygiene (P5.5): what may never appear in suggestions or popular searches, whoever searched it.
# The patterns run on normalised queries, which keep digits, dots and slashes but drop @ and colons.
# A phone number: seven or more digits joined only by dashes, dots or brackets ("080-2345-6789", "9845012345"),
# or, when spaces split the digits, ten or more digits starting with 0 or 6 to 9, the way Indian mobile and
# STD numbers start ("98450 12345", "080 2345 6789"). So "iphone 15 256 2023" (a model, a size and a year)
# is not a phone number.
_PHONE = re.compile(r"(?<!\d)\d(?:[().-]*\d){6,}|(?<!\d)[06-9](?:[\s().-]*\d){9,}")
# An email address, or a mail provider's domain ("ravi gmail.com" once the @ is dropped); a provider's bare
# name ("yahoo", "outlook diary") is fine.
_MAIL_PROVIDERS = r"(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|rediffmail|icloud|protonmail)"
_EMAIL = re.compile(rf"[^\s@]+@[^\s@]+|\b{_MAIL_PROVIDERS}\s*(?:\.|\bdot\b|\bcom\b)", re.IGNORECASE)
# A link: a scheme, www., a messaging short link, a domain followed by a path, or a domain on a spam-prone
# top-level domain. A shop or brand domain on its own ("amazon.in gift card") is fine.
_LINK = re.compile(r"\bhttps?\b|\bwww\.|\bt\.me\b|\bwa\.me\b|[a-z0-9]\.[a-z]{2,6}/|"
                   r"[a-z0-9]\.(?:xyz|ly|link|me|io|info|biz|top|club|site|online|click)\b", re.IGNORECASE)
# Abusive words in English, and in Hindi, Kannada and Telugu, in Latin letters and in their own scripts.
# A Latin word matches as a whole word, with or without a common ending ("fucking", "bastards"), so
# "rapeseed" or "dickies" are not caught. A word in _ABUSIVE_EXACT matches only as itself, as its endings make
# ordinary words ("sexy dress", "lunda"). Words that are also ordinary shopping terms ("nude" lipstick, "dengue"
# test kit) are left out, and _SAFE_PHRASES ("rape seed oil") are removed before matching. A word in an Indian
# script matches anywhere, as those scripts join suffixes to the word. NEARSHOP_SUGGESTION_BLOCKLIST adds
# words without a deploy.
_ABUSIVE_LATIN = (
    "fuck", "fucker", "motherfuck", "motherfucker", "shit", "bitch", "bastard", "asshole", "dick", "cunt", "slut",
    "whore", "porn", "rape", "rapist", "nigger", "faggot",
    "chutiya", "chutiye", "chutia", "madarchod", "maderchod", "behenchod", "bhenchod", "benchod", "bhosdi",
    "bhosdike", "bhosad", "bhosadi", "gandu", "randi", "harami", "lavda", "lauda", "kamina", "kamine",
    "sule", "soole", "bolimaga", "boli maga", "sulemaga", "soolemaga", "tunne", "nin amman", "ninamman",
    "lanja", "dengey", "dengu ra", "puku", "modda", "kojja",
)
_ABUSIVE_EXACT = ("sex", "lund")
_SAFE_PHRASES = re.compile(r"(?<![a-z0-9])rape\s*seeds?(?![a-z0-9])", re.IGNORECASE)
_ABUSIVE_SCRIPT = (
    "चूतिया", "चुतिया", "मादरचोद", "बहनचोद", "भेनचोद", "भोसड", "गांडू", "रंडी", "हरामी", "लौड़ा",
    "ಸೂಳೆ", "ಬೋಳಿಮಗ", "ತುಣ್ಣೆ", "ಸೂಳೆಮಗ",
    "లంజ", "దెంగ", "పూకు", "మొడ్డ", "కొజ్జ",
)
_ENDINGS = r"(?:s|es|er|ers|ing|in|ed|y|a|e|i|o)?"


@lru_cache(maxsize=4)
def _blocklist(configured: str) -> tuple[re.Pattern[str], tuple[str, ...]]:
    """The abusive-word matchers, with the configured extra words (NEARSHOP_SUGGESTION_BLOCKLIST)."""
    extra = [w.strip().lower() for w in configured.split(",") if w.strip()]
    latin = sorted({w for w in (*_ABUSIVE_LATIN, *extra) if w.isascii()}, key=len, reverse=True)
    script = (*_ABUSIVE_SCRIPT, *(w for w in extra if not w.isascii()))
    words = "|".join(re.escape(w) for w in latin)
    exact = "|".join(re.escape(w) for w in _ABUSIVE_EXACT)
    return re.compile(rf"(?<![a-z0-9])(?:(?:{words}){_ENDINGS}|{exact})(?![a-z0-9])", re.IGNORECASE), script


def suggestion_allowed(query: str) -> bool:
    """Whether a query's text may appear in public suggestions: at most suggestion_max_chars characters, and no
    phone number, email address, link or abusive word."""
    text = (query or "").strip()
    if not text or len(text) > settings.suggestion_max_chars:
        return False
    if _PHONE.search(text) or _EMAIL.search(text) or _LINK.search(text):
        return False
    latin, script = _blocklist(settings.suggestion_blocklist)
    folded = _SAFE_PHRASES.sub(" ", text.lower())
    return not latin.search(folded) and not any(w in folded for w in script)


def _top_queries(db: Database, match: dict, limit: int) -> list[str]:
    """The most searched normalised queries that some result answered, counting distinct signed-in users only:
    a query qualifies when suggestion_min_users or more users (by user_id, never by session or device)
    searched it within suggestion_window_days, and its text passes suggestion_allowed."""
    since = utcnow() - timedelta(days=settings.suggestion_window_days)
    rows = db.search_history.aggregate([
        {"$match": {**match, "results_count": {"$gt": 0}, "user_id": {"$ne": None}, "created_at": {"$gte": since}}},
        {"$group": {"_id": "$normalized_query", "users": {"$addToSet": "$user_id"}, "n": {"$sum": 1}}},
        {"$project": {"n": 1, "u": {"$size": "$users"}}},
        {"$match": {"u": {"$gte": settings.suggestion_min_users}}},
        {"$sort": {"u": -1, "n": -1, "_id": 1}},
        {"$limit": limit * 5},  # room for the ones the blocklist drops
    ])
    return [r["_id"] for r in rows if isinstance(r["_id"], str) and suggestion_allowed(r["_id"])][:limit]


def _suggested_items(db: Database, norm: str, limit: int, keyword_engine: str) -> list[dict]:
    """Catalogue items for suggestions: the Atlas autocomplete index when the keyword engine is atlas_search (P5.7),
    otherwise a substring match on the name and a prefix match on the brand. Raw documents."""
    if keyword_engine == "atlas_search":
        return atlas_search.autocomplete_items(db, norm, limit)
    contains = {"$regex": re.escape(norm), "$options": "i"}
    starts = {"$regex": f"^{re.escape(norm)}", "$options": "i"}
    return db.catalog_items.find_raw({"$or": [{"name": contains}, {"brand": starts}]},
                                     {"name": 1, "brand": 1, "icon": 1, "category_id": 1}, limit=limit)


def suggestions(db: Database, q: str, limit: int = 8, engines: EnginePair | None = None) -> dict:
    norm = normalize_query(q)
    if len(norm) < 2:
        return {"queries": [], "items": [], "categories": []}
    keyword_engine = (engines or settings.search_engines).keyword
    cats = categories(db)
    queries, items = db.gather(
        # Anchored, case-sensitive prefix on normalised (lower-case) text: served by the index.
        lambda: _top_queries(db, {"normalized_query": {"$regex": f"^{re.escape(norm)}"}}, 4),
        lambda: _suggested_items(db, norm, limit, keyword_engine),
    )
    return {
        "queries": queries,
        "items": [{"id": i["_id"], "name": i["name"], "brand": i.get("brand"), "icon": i.get("icon", "Package"),
                   "category": cats[i["category_id"]].slug if i.get("category_id") in cats else None}
                  for i in items],
        "categories": [{"slug": c.slug, "name": c.name, "icon": c.icon} for c in cats.values()
                       if norm in c.name.lower()],
    }


def popular_searches(db: Database, limit: int = 8) -> list[str]:
    return _top_queries(db, {}, limit)


def recent_searches(db: Database, user_id: int, limit: int = 6) -> list[str]:
    return [r["_id"] for r in db.search_history.aggregate([
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$normalized_query", "t": {"$max": "$created_at"}}},
        {"$sort": {"t": -1}},
        {"$limit": limit},
    ])]


def reset_vocabulary() -> None:
    """Forget this process's vocabularies (tests and the evaluation harness): rebuilt on the next search."""
    _vocab.clear()
