"""Search evaluation harness (P5.1; plan section 8.6). Search quality as a number per language.

    make eval                               # the dev split, on the local MongoDB
    make eval EVAL_SPLIT=test               # the held-out test split: once per tuning cycle
    uv run python -m app.ai.search_eval --help

Every labelled query in backend/eval/queries.<split>.jsonl (format in backend/eval/README.md) goes
through run_search exactly as a customer's search would (no logging), over a fixed evaluation database
built from the deterministic seed. The metrics, overall and per language:

  - nDCG@10, graded: gain 2^grade - 1, discount log2(rank + 1); the ideal ranking is built from the
    judged items that some shop inside the query's radius lists.
  - Recall@20: relevant items (grade 2 or more) in the top 20, over the relevant items in radius.
  - MRR: 1 / rank of the first relevant item (0 when none is in the top 20).
  - Zero-result rate: queries with no result, over the queries with a relevant item in radius.
  - Latency p95 (nearest rank) of run_search, in milliseconds, over every query.

nDCG, recall, MRR and the zero-result rate count only "scored" queries, those with a relevant item in
radius; a query whose relevant items no nearby shop lists says nothing about ranking.

The evaluation database (default nearshop_eval) is a seeded snapshot on a local MongoDB only. It is
built on the first run and reused while the seed, its data files, the schema (indexes), the migrations
and the models are unchanged (a fingerprint is kept in its eval_snapshot collection); --rebuild forces a
new one. Each run is stored in that database's model_runs as an entry named search_eval, with the split
and the git commit in its metrics.

Because no run can be stored in a deployed database, --publish also writes each split's figures to the
committed file backend/eval/published.json, which the AI Lab's endpoint reads on every deployment.

Engines (P5.7; app/ai/atlas_search.py, docs/search/engine-decision.md)
---------------------------------------------------------------------
--keyword-engine (text or atlas_search) and --vector-engine (in_process or atlas_vector) run any engine pair on
either split; the defaults are the configured engines. Each run also records, overall and per language:

  - the autocomplete hit rate: for the first 3 and the first 5 characters of each query with a grade-3 item, a hit
    when a grade-3 item is among the top 8 suggested items (suggestions, with the run's keyword engine);
  - database operations per search (every command the driver sends during run_search, counted by a command
    listener on the harness's own client; handshakes, pings and authentication are not counted);
  - the process's resident memory after the run and its peak, and the database's storage (documents and
    indexes; Atlas does not report search index sizes to the driver: read them in the Atlas UI).

--scale N copies the seed catalogue across synthetic shops until the evaluation database holds N active listings
(50,000 for city scale), once per evaluation database. Like the seed, it runs only where the P1.1 environment
guard (database/schema.py, check_reset_allowed) allows a seed: development, test and demo, never staging or
production. The guard decides, not the database's or the cluster's name.

This module changes no ranking: it only calls run_search.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import resource
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pymongo import MongoClient, monitoring
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.core.config import BACKEND_DIR, KEYWORD_ENGINES, VECTOR_ENGINES, EnginePair, settings
from app.core.database import Database, utcnow
from app.schemas.serializers import iso

LANGUAGES = ("en", "kn", "kn-Latn", "te", "te-Latn", "hi", "hi-Latn", "misspelt")
SPLITS = ("dev", "test")
GRADES = (0, 1, 2, 3)  # 0 irrelevant, 1 related, 2 good substitute, 3 exact
RELEVANT_GRADE = 2
NDCG_K = 10
RECALL_K = 20
MAX_DEV_DROP = 0.02  # CI fails a pull request that lowers dev nDCG@10 by more than this (plan section 8.6)
EVAL_DIR = BACKEND_DIR / "eval"
EVAL_DB_PREFIX = "nearshop_eval"
SNAPSHOT_COLLECTION = "eval_snapshot"
RUN_NAME = "search_eval"
SEED_DIR = BACKEND_DIR / "database" / "seed"
# Everything the seed builds the evaluation database from: a change to any of these rebuilds the snapshot.
FINGERPRINT_GLOBS = (
    "database/seed/seed.py", "database/seed/data/*.json", "database/schema.py", "database/migrations/*.py",
    "app/models/*.py",
)
PUBLISHED_PATH = EVAL_DIR / "published.json"  # committed figures the deployed AI Lab shows (--publish)
EVAL_ENVIRONMENTS = ("development", "test")  # a local MongoDB only (database/schema.py, LOCAL_HOSTS)
METRIC_KEYS = ("ndcg@10", "recall@20", "mrr", "zero_result_rate", "latency_p95_ms")
AUTOCOMPLETE_PREFIXES = (3, 5)
AUTOCOMPLETE_TOP = 8
EXACT_GRADE = 3
SCALE_SEED = 5007  # the synthetic shops' placement is reproducible
SCALE_JITTER_DEG = 0.02  # a synthetic shop sits within about 2 km of the shop it copies


class LabelError(ValueError):
    """A labelled query file that does not follow backend/eval/README.md."""


@dataclass(frozen=True)
class LabelledQuery:
    query_id: str
    text: str
    lang: str
    lat: float
    lng: float
    radius_km: float
    judgments: dict[int, int]  # catalog item id -> grade
    synthetic: bool = False


@dataclass
class QueryOutcome:
    query_id: str
    lang: str
    has_relevant_in_radius: bool
    ranked: list[int | None] = field(default_factory=list)  # catalog item id per result group, best first
    total: int = 0
    latency_ms: float = 0.0
    ndcg: float = 0.0
    recall: float = 0.0
    rr: float = 0.0
    db_ops: int | None = None  # database commands run_search sent (None: not counted)


# ---------------------------------------------------------------- labels

def parse_query(row: Mapping[str, Any], where: str = "row") -> LabelledQuery:
    def need(key: str, kind: type | tuple[type, ...]) -> Any:
        if key not in row:
            raise LabelError(f"{where}: missing {key}")
        value = row[key]
        if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
            raise LabelError(f"{where}: {key} has the wrong type")
        return value

    query_id = need("query_id", str).strip()
    text = need("text", str).strip()
    lang = need("lang", str)
    lat, lng, radius = (float(need(k, (int, float))) for k in ("lat", "lng", "radius_km"))
    raw = need("judgments", dict)
    if not query_id or not text:
        raise LabelError(f"{where}: query_id and text must not be empty")
    if lang not in LANGUAGES:
        raise LabelError(f"{where}: lang {lang!r} is not one of {', '.join(LANGUAGES)}")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or radius <= 0:
        raise LabelError(f"{where}: lat, lng or radius_km is out of range")
    judgments: dict[int, int] = {}
    for key, grade in raw.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            raise LabelError(f"{where}: judgment key {key!r} is not a catalog item id") from None
        if isinstance(grade, bool) or grade not in GRADES:
            raise LabelError(f"{where}: grade {grade!r} for item {key} is not one of {GRADES}")
        judgments[item_id] = int(grade)
    if not judgments:
        raise LabelError(f"{where}: no judgments")
    return LabelledQuery(query_id, text, lang, lat, lng, radius, judgments, bool(row.get("synthetic", False)))


def load_queries(path: Path) -> list[LabelledQuery]:
    """Parse a JSONL label file; blank lines are skipped. Raises LabelError on any bad row or a repeated id."""
    out: list[LabelledQuery] = []
    seen: set[str] = set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        where = f"{Path(path).name}:{n}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise LabelError(f"{where}: not JSON ({e.msg})") from None
        if not isinstance(row, dict):
            raise LabelError(f"{where}: not a JSON object")
        q = parse_query(row, where)
        if q.query_id in seen:
            raise LabelError(f"{where}: query_id {q.query_id!r} repeats")
        seen.add(q.query_id)
        out.append(q)
    return out


def split_path(split: str, queries_dir: Path = EVAL_DIR) -> Path:
    return Path(queries_dir) / f"queries.{split}.jsonl"


# ---------------------------------------------------------------- metrics (pure)

def dcg(grades: Iterable[int]) -> float:
    return sum((2 ** g - 1) / math.log2(rank + 1) for rank, g in enumerate(grades, 1))


def ndcg_at_k(ranked: Sequence[int | None], judgments: Mapping[int, int], k: int = NDCG_K,
              candidates: Iterable[int] | None = None) -> float:
    """Graded nDCG@k. The ideal ranking uses the judged items in `candidates` (all judged items when None)."""
    if candidates is None:
        pool = dict(judgments)
    else:
        allowed = set(candidates)  # once: `candidates` may be a one-shot iterator
        pool = {i: g for i, g in judgments.items() if i in allowed}
    ideal = dcg(sorted(pool.values(), reverse=True)[:k])
    if ideal <= 0:
        return 0.0
    return dcg(judgments.get(i, 0) if i is not None else 0 for i in ranked[:k]) / ideal


def recall_at_k(ranked: Sequence[int | None], judgments: Mapping[int, int], k: int = RECALL_K,
                candidates: Iterable[int] | None = None) -> float:
    relevant = {i for i, g in judgments.items() if g >= RELEVANT_GRADE}
    if candidates is not None:
        relevant &= set(candidates)
    if not relevant:
        return 0.0
    return len(relevant & {i for i in ranked[:k] if i is not None}) / len(relevant)


def reciprocal_rank(ranked: Sequence[int | None], judgments: Mapping[int, int]) -> float:
    for rank, item in enumerate(ranked, 1):
        if item is not None and judgments.get(item, 0) >= RELEVANT_GRADE:
            return 1 / rank
    return 0.0


def percentile(values: Sequence[float], pct: float = 95) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(pct / 100 * len(ordered)) - 1, 0)]


def score_query(q: LabelledQuery, ranked: Sequence[int | None], total: int, latency_ms: float,
                available: Iterable[int] | None = None) -> QueryOutcome:
    """One query's outcome. `available` is the set of judged catalog items some shop in radius lists
    (None: assume every judged item is available)."""
    avail = set(q.judgments) if available is None else set(available) & set(q.judgments)
    has_relevant = any(q.judgments[i] >= RELEVANT_GRADE for i in avail)
    ranked = list(ranked)
    return QueryOutcome(
        query_id=q.query_id, lang=q.lang, has_relevant_in_radius=has_relevant, ranked=ranked, total=total,
        latency_ms=latency_ms,
        ndcg=ndcg_at_k(ranked, q.judgments, NDCG_K, avail) if has_relevant else 0.0,
        recall=recall_at_k(ranked, q.judgments, RECALL_K, avail) if has_relevant else 0.0,
        rr=reciprocal_rank(ranked[:RECALL_K], q.judgments) if has_relevant else 0.0,
    )


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarise(outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    scored = [o for o in outcomes if o.has_relevant_in_radius]
    p95 = percentile([o.latency_ms for o in outcomes])
    ops = [float(o.db_ops) for o in outcomes if o.db_ops is not None]
    return {
        "queries": len(outcomes),
        "scored_queries": len(scored),
        "ndcg@10": _mean([o.ndcg for o in scored]),
        "recall@20": _mean([o.recall for o in scored]),
        "mrr": _mean([o.rr for o in scored]),
        "zero_result_rate": _mean([1.0 if o.total == 0 else 0.0 for o in scored]),
        "latency_p95_ms": round(p95, 1) if p95 is not None else None,
        "db_ops_per_search": _mean(ops),
    }


def aggregate(outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    """Overall and per-language figures; languages in LANGUAGES order, only those with queries."""
    by_lang: dict[str, list[QueryOutcome]] = {}
    for o in outcomes:
        by_lang.setdefault(o.lang, []).append(o)
    order = {lang: n for n, lang in enumerate(LANGUAGES)}
    return {
        "overall": summarise(outcomes),
        "by_language": {lang: summarise(by_lang[lang]) for lang in sorted(by_lang, key=lambda x: order.get(x, 99))},
    }


def autocomplete_cases(q: LabelledQuery) -> list[str]:
    """The prefixes of one query that the autocomplete hit rate tries: its first 3 and first 5 characters, each only
    when the query is that long, and only for a query with a grade-3 item."""
    if not any(g >= EXACT_GRADE for g in q.judgments.values()):
        return []
    return [q.text[:n] for n in AUTOCOMPLETE_PREFIXES if len(q.text) >= n]


def autocomplete_hit(q: LabelledQuery, suggested: Sequence[int]) -> bool:
    """A hit when a grade-3 item is among the top AUTOCOMPLETE_TOP suggested items."""
    return any(q.judgments.get(i, 0) >= EXACT_GRADE for i in list(suggested)[:AUTOCOMPLETE_TOP])


def autocomplete_rates(rows: Sequence[tuple[str, bool]]) -> dict[str, Any]:
    """Hit rates from (language, hit) pairs, one per prefix tried: overall and per language, in LANGUAGES order."""
    def rate(hits: list[bool]) -> dict[str, Any]:
        return {"prefixes": len(hits), "hit_rate": _mean([1.0 if h else 0.0 for h in hits])}

    by_lang: dict[str, list[bool]] = {}
    for lang, hit in rows:
        by_lang.setdefault(lang, []).append(hit)
    order = {lang: n for n, lang in enumerate(LANGUAGES)}
    return {"overall": rate([h for _, h in rows]),
            "by_language": {lang: rate(by_lang[lang]) for lang in sorted(by_lang, key=lambda x: order.get(x, 99))}}


def compare(current: Mapping[str, Any], baseline: Mapping[str, Any] | None,
            max_drop: float = MAX_DEV_DROP) -> tuple[bool, str]:
    """Overall nDCG@10 of `current` against `baseline` (aggregate() results). False when it fell by more than
    max_drop. A missing baseline or figure passes, with a note."""
    cur = (current.get("overall") or {}).get("ndcg@10")
    base = ((baseline or {}).get("overall") or {}).get("ndcg@10")
    if base is None:
        return True, "No baseline nDCG@10 to compare with (no earlier main run): passes."
    if cur is None:
        return True, "This run has no scored query, so there is no nDCG@10 to compare: passes."
    delta = cur - base
    ok = delta >= -max_drop - 1e-9
    verdict = "passes" if ok else f"FAILS: dropped by more than {max_drop}"
    return ok, f"nDCG@10 {cur:.4f} against {base:.4f} on main ({delta:+.4f}): {verdict}."


# ---------------------------------------------------------------- running against the evaluation database

def eval_db_problem(*, environment: str, mongodb_uri: str, db_name: str, production_db: str,
                    protected_hosts: list[str]) -> str | None:
    """Say why `db_name` must not serve as the evaluation database, or return None. Pure."""
    from database import schema

    if not db_name.startswith(EVAL_DB_PREFIX):
        return (f"NEARSHOP_MONGODB_DB is {db_name!r}; the evaluation database's name starts with "
                f"{EVAL_DB_PREFIX!r} (run make eval).")
    if environment not in EVAL_ENVIRONMENTS:
        return f"NEARSHOP_ENVIRONMENT is {environment!r}; evaluation runs only in {', '.join(EVAL_ENVIRONMENTS)}."
    return schema.check_reset_allowed(environment=environment, mongodb_uri=mongodb_uri, db_name=db_name,
                                      production_db=production_db, protected_hosts=protected_hosts,
                                      confirm=db_name)


def fingerprint_files(root: Path = BACKEND_DIR) -> list[Path]:
    return sorted({path for pattern in FINGERPRINT_GLOBS for path in Path(root).glob(pattern) if path.is_file()})


def seed_fingerprint(root: Path = BACKEND_DIR) -> str:
    """Changes whenever the seed, its data files, the schema (indexes, the products text index included), the
    migrations or the models change, so a stale snapshot is rebuilt."""
    h = hashlib.sha256(settings.seed_city.encode())
    for path in fingerprint_files(root):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def ensure_snapshot(db: Database, rebuild: bool = False) -> str:
    """Reuse the seeded evaluation database while its fingerprint matches, else re-seed it. Returns the fingerprint.
    Either way, search's freshness clock (P7.4) is fixed to the snapshot's seeding time, so stock freshness is judged
    as it was when the snapshot was built, however old the snapshot is."""
    from app.services.search import set_clock

    fingerprint = seed_fingerprint()
    meta = db.mongo[SNAPSHOT_COLLECTION]
    current = meta.find_one({"_id": "snapshot"})
    if current and current.get("fingerprint") == fingerprint and not rebuild:
        print(f"Reusing the evaluation database {db.mongo.name!r} (seeded {current.get('built_at')}).")
        set_clock(current.get("built_at"))
        return fingerprint
    from database.seed import seed  # imported on use: it loads the training libraries

    meta.delete_many({})  # an interrupted build must not be reused
    # Nobody signs in to the evaluation database: its demo accounts get a fresh random password.
    settings.demo_password = SecretStr(secrets.token_urlsafe(24))
    print(f"Seeding the evaluation database {db.mongo.name!r} (about two minutes)...")
    try:
        seed.main(["--confirm-db", db.mongo.name])
    except DuplicateKeyError:
        # The seed draws reservation and order codes at random (app/utils/text.py short_code); a rare clash
        # aborts it. The seed starts by resetting the database, so one more attempt starts clean.
        print("The seed drew a duplicate code; seeding once more...")
        seed.main(["--confirm-db", db.mongo.name])
    built_at = utcnow()
    meta.insert_one({"_id": "snapshot", "fingerprint": fingerprint, "seed_city": settings.seed_city,
                     "built_at": built_at})
    set_clock(built_at)
    return fingerprint


def available_items(db: Database, q: LabelledQuery) -> set[int]:
    """The judged catalog items that an active listing in a shop inside the query's radius carries."""
    from app.services.search import nearby_shops

    shops = nearby_shops(db, q.lat, q.lng, q.radius_km)
    if not shops:
        return set()
    rows = db.products.find_raw({"is_active": True, "shop_id": {"$in": list(shops)},
                                 "catalog_item_id": {"$in": list(q.judgments)}}, {"catalog_item_id": 1})
    return {r["catalog_item_id"] for r in rows}


class CommandCounter(monitoring.CommandListener):
    """Counts the database commands a client sends, apart from handshakes, pings, authentication and session ends.
    Each counts as an operation on Atlas's operations-per-second bands."""

    IGNORED = frozenset({"hello", "ismaster", "isMaster", "ping", "buildinfo", "buildInfo", "saslStart",
                         "saslContinue", "authenticate", "endSessions", "getnonce"})

    def __init__(self) -> None:
        self.count = 0
        self._lock = threading.Lock()

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if event.command_name not in self.IGNORED:
            with self._lock:
                self.count += 1

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        pass

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        pass


def install_command_counter() -> CommandCounter:
    """Give the app a client of its own with a command counter (the evaluation process only). Database handles
    created afterwards use it."""
    from app.core import database

    counter = CommandCounter()
    database.client = MongoClient(settings.mongodb_uri, appname="nearshop-eval", serverSelectionTimeoutMS=15000,
                                  retryWrites=True, tz_aware=False, event_listeners=[counter])
    return counter


def evaluate(db: Database, queries: Sequence[LabelledQuery], *, ranking: Any = None, cache: dict | None = None,
             available: dict[str, set[int]] | None = None, prepare: bool = True, engines: EnginePair | None = None,
             counter: CommandCounter | None = None) -> list[QueryOutcome]:
    """Run every query through run_search (unlogged) and score it.

    ranking: a RankingConfig to rank with instead of the configured one (P5.5). cache and available let the
    tuning harness (app/ai/search_tuning.py) run many configs over one prepared set: cache is run_search's
    fetch-stage memo, available the judged items in radius per query id (filled in when missing), and
    prepare=False skips rebuilding the semantic index and the warm-up. Latency is not meaningful with a cache.
    engines: the engine pair to search with (the configured one when None). counter: the command counter of the
    client db uses (install_command_counter), to count each search's database operations.
    """
    from app.ai.semantic_index import index
    from app.services.search import SearchParams, reset_vocabulary, run_search

    def params(q: LabelledQuery) -> SearchParams:
        return SearchParams(q=q.text, lat=q.lat, lng=q.lng, radius_km=q.radius_km, page_size=RECALL_K)

    if prepare:
        index.mark_dirty()
        index.ensure(db)
        reset_vocabulary()
        if queries:  # warm-up, untimed: the vocabulary and caches load on the first search
            run_search(db, params(queries[0]), log=False, ranking=ranking, engines=engines)
    avail = {} if available is None else available
    out = []
    for q in queries:
        ops_before = counter.count if counter is not None else 0
        started = time.perf_counter()
        res = run_search(db, params(q), log=False, ranking=ranking, cache=cache, engines=engines)
        latency = (time.perf_counter() - started) * 1000
        ops = counter.count - ops_before if counter is not None else None
        ranked = [g["catalog_item_id"] for g in res["groups"]]
        if q.query_id not in avail:
            avail[q.query_id] = available_items(db, q)
        outcome = score_query(q, ranked, res["total"], latency, avail[q.query_id])
        outcome.db_ops = ops
        out.append(outcome)
    return out


def evaluate_autocomplete(db: Database, queries: Sequence[LabelledQuery],
                          engines: EnginePair | None = None) -> dict[str, Any]:
    """The autocomplete hit rate (autocomplete_cases, autocomplete_hit), overall and per language."""
    from app.services.search import suggestions

    rows = []
    for q in queries:
        for prefix in autocomplete_cases(q):
            items = suggestions(db, prefix, limit=AUTOCOMPLETE_TOP, engines=engines)["items"]
            rows.append((q.lang, autocomplete_hit(q, [i["id"] for i in items])))
    return autocomplete_rates(rows)


def resources(db: Database) -> dict[str, Any]:
    """This process's resident memory now and at its peak, in MB, and the database's storage: documents and indexes
    (search indexes not included: Atlas does not report their size to the driver)."""
    from app.services.analytics import collection_sizes

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # bytes on macOS, KB on Linux
    try:
        import psutil

        rss_mb: float | None = psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # psutil missing: the peak is still known
        rss_mb = None
    try:
        sizes = collection_sizes(db.mongo)
        data = sum(c["data_bytes"] for c in sizes)
        index_bytes = sum(c["index_bytes"] for c in sizes)
        storage: dict[str, Any] = {"data_mb": round(data / 2**20, 1), "index_mb": round(index_bytes / 2**20, 1),
                                   "total_mb": round((data + index_bytes) / 2**20, 1),
                                   "search_indexes": "not reported to the driver; read them in the Atlas UI"}
    except PyMongoError as exc:
        storage = {"error": str(exc)[:200]}
    listings = db.products.count({"is_active": True})
    return {"rss_mb_after_load": round(rss_mb, 1) if rss_mb is not None else None, "peak_rss_mb": round(peak_mb, 1),
            "active_listings": listings, "storage": storage}


# ---------------------------------------------------------------- the scale option


def scale_problem(db: Database) -> str | None:
    """Why the catalogue must not be scaled in this database, or None: the P1.1 guard for a seed."""
    from database import schema

    return schema.reset_problem(db.mongo.name, db.mongo.name, client=db.mongo.client)


def scale_listings(db: Database, target: int) -> int:
    """Copy the seed's shops and their active listings, as synthetic shops near the shops they copy, until the
    database holds `target` active listings. Returns the number of listings added (0 when it holds that many).
    Refused (ValueError) where the P1.1 guard does not allow a seed."""
    from app.ai.semantic_index import index
    from app.core import cache_version

    if problem := scale_problem(db):
        raise ValueError(f"Scaling refused; nothing was written. {problem}")
    have = db.products.count({"is_active": True})
    need = target - have
    if need <= 0:
        return 0
    shops = db.shops.find_raw({"is_active": True}, sort=[("_id", 1)])
    by_shop: dict[int, list[dict]] = {}
    for row in db.products.find_raw({"is_active": True}, sort=[("_id", 1)]):
        by_shop.setdefault(row["shop_id"], []).append(row)
    templates = [s for s in shops if by_shop.get(s["_id"])]
    if not templates:
        raise ValueError("No shop with active listings to copy: seed the database first.")
    rng = random.Random(SCALE_SEED)
    now = utcnow()
    added = n = 0
    while added < need:
        template = templates[n % len(templates)]
        n += 1
        listings = by_shop[template["_id"]][: need - added]
        shop_id = db.next_id("shops")
        lng, lat = template["location"]["coordinates"]
        shop = {**template, "_id": shop_id, "slug": f"{template['slug']}-scale-{shop_id}",
                "name": f"{template['name']} (synthetic {shop_id})",
                "location": {"type": "Point", "coordinates": [lng + rng.uniform(-SCALE_JITTER_DEG, SCALE_JITTER_DEG),
                                                              lat + rng.uniform(-SCALE_JITTER_DEG, SCALE_JITTER_DEG)]},
                "created_at": now, "updated_at": now}
        ids = db.next_ids("products", len(listings))
        db.shops.raw.insert_one(shop)
        db.products.raw.insert_many([{**row, "_id": pid, "shop_id": shop_id, "created_at": now, "updated_at": now}
                                     for pid, row in zip(ids, listings, strict=True)], ordered=False)
        added += len(listings)
    cache_version.bump(db.mongo, cache_version.LISTINGS)
    index.mark_dirty()
    return added


def git_commit() -> tuple[str, bool]:
    """The checked-out commit and whether the working tree has uncommitted changes."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=10,
                              check=True).stdout.strip()

    try:
        return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        return "unknown", False


def record_run(db: Database, *, split: str, result: Mapping[str, Any], queries: Sequence[LabelledQuery],
               queries_file: str, commit: str, dirty: bool, snapshot: str, duration_ms: int):
    from app.ai.semantic_index import index
    from app.models import ModelRun

    synthetic = sum(q.synthetic for q in queries)
    ranking = settings.ranking.version
    run = ModelRun(
        name=RUN_NAME,
        algorithm=f"run_search, {index.provider_name or 'unknown'} embeddings, ranking {ranking}",
        n_samples=len(queries),
        metrics={"split": split, "commit": commit, "commit_dirty": dirty, "queries_file": queries_file,
                 "synthetic_queries": synthetic, "snapshot": snapshot[:12], "embedding": index.provider_name,
                 "ranking": ranking, **result},
        data_note=(f"{len(queries)} labelled {split} queries ({synthetic} synthetic examples) on the seeded "
                   f"evaluation snapshot."),
        uses_demo_data=True,
        duration_ms=duration_ms,
    )
    db.model_runs.insert(run)
    return run


def run_split(db: Database, split: str, *, queries_dir: Path = EVAL_DIR, store: bool = True,
              snapshot: str = "", engines: EnginePair | None = None,
              counter: CommandCounter | None = None) -> dict[str, Any]:
    path = split_path(split, queries_dir)
    queries = load_queries(path)
    started = time.time()
    engines = engines or settings.search_engines
    outcomes = evaluate(db, queries, engines=engines, counter=counter)
    result = {**aggregate(outcomes), "engines": {"keyword": engines.keyword, "vector": engines.vector},
              "autocomplete": evaluate_autocomplete(db, queries, engines), "resources": resources(db)}
    commit, dirty = git_commit()
    stored = None
    if store:
        try:
            queries_file = str(path.resolve().relative_to(BACKEND_DIR))
        except ValueError:
            queries_file = str(path)
        stored = record_run(db, split=split, result=result, queries=queries, queries_file=queries_file,
                            commit=commit, dirty=dirty, snapshot=snapshot,
                            duration_ms=int((time.time() - started) * 1000))
    return {"split": split, "commit": commit, "commit_dirty": dirty, "stored": stored, **result,
            "per_query": [{k: v for k, v in asdict(o).items() if k != "ranked"} | {"top10": o.ranked[:10]}
                          for o in outcomes]}


# ---------------------------------------------------------------- what the AI Lab shows

def run_card(run: Any) -> dict[str, Any]:
    """The AI Lab's view of one stored search_eval run (a ModelRun)."""
    m = run.metrics
    return {
        "split": m.get("split"), "at": iso(run.created_at), "commit": str(m.get("commit") or "")[:10],
        "queries": run.n_samples, "synthetic_queries": m.get("synthetic_queries", 0),
        "embedding": m.get("embedding"), "ranking": m.get("ranking"), "overall": m.get("overall") or {},
        "by_language": m.get("by_language") or {}, "note": run.data_note, "source": "database",
    }


def load_published(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The committed figures per split ({} when the file is missing or unreadable)."""
    try:
        data = json.loads(Path(path or PUBLISHED_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    splits = data.get("splits") if isinstance(data, dict) else None
    if not isinstance(splits, dict):
        return {}
    return {s: card for s, card in splits.items() if s in SPLITS and isinstance(card, dict)}


def publish(runs: Sequence[Any], path: Path | None = None) -> None:
    """Write the cards of these stored runs to the committed file, keeping the other split's entry."""
    splits = load_published(path)
    for run in runs:
        splits[run.metrics["split"]] = run_card(run) | {"source": "published"}
    body = {"about": "Search evaluation figures published by `make eval EVAL_ARGS=--publish` "
                     "(backend/eval/README.md). The AI Lab shows them on every deployment.",
            "splits": {s: splits[s] for s in SPLITS if s in splits}}
    Path(path or PUBLISHED_PATH).write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def latest_cards(db: Database, published_path: Path | None = None) -> dict[str, dict[str, Any] | None]:
    """The latest figures for each split: the newer of the latest stored run in this database and the published
    entry; None for a split never measured."""
    published = load_published(published_path)
    out: dict[str, dict[str, Any] | None] = {}
    for split in SPLITS:
        runs = db.model_runs.find({"name": RUN_NAME, "metrics.split": split}, sort=[("created_at", -1)], limit=1)
        candidates = [c for c in (run_card(runs[0]) if runs else None, published.get(split)) if c]
        out[split] = max(candidates, key=lambda c: str(c.get("at") or "")) if candidates else None
    return out


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def summary_markdown(runs: Sequence[Mapping[str, Any]], notes: Sequence[str] = ()) -> str:
    lines = ["## Search evaluation", ""]
    for run in runs:
        engines = run.get("engines") or {}
        engine_note = f", engines {engines['keyword']} + {engines['vector']}" if engines else ""
        lines += [f"**{run['split']} split** at {str(run['commit'])[:10]}{' (uncommitted changes)' if run.get('commit_dirty') else ''}"
                  f"{engine_note}",
                  "", "| Language | Queries | Scored | nDCG@10 | Recall@20 | MRR | Zero-result | p95 ms | Ops/search |"
                  " Autocomplete |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        auto = run.get("autocomplete") or {}
        auto_rows = {"All": auto.get("overall") or {}, **(auto.get("by_language") or {})}
        rows = [("All", run["overall"]), *run["by_language"].items()]
        for name, m in rows:
            lines.append(f"| {name} | {m['queries']} | {m['scored_queries']} | {_fmt(m['ndcg@10'])} | "
                         f"{_fmt(m['recall@20'])} | {_fmt(m['mrr'])} | {_fmt(m['zero_result_rate'])} | "
                         f"{_fmt(m['latency_p95_ms'], 0)} | {_fmt(m.get('db_ops_per_search'), 1)} | "
                         f"{_fmt((auto_rows.get(name) or {}).get('hit_rate'))} |")
        if res := run.get("resources"):
            storage = res.get("storage") or {}
            lines += ["", f"Memory: {_fmt(res.get('rss_mb_after_load'), 0)} MB resident after the run, "
                          f"{_fmt(res.get('peak_rss_mb'), 0)} MB peak. Storage: {storage.get('total_mb', '-')} MB "
                          f"(documents and indexes, without search indexes) for {res.get('active_listings')} active "
                          f"listings."]
        lines.append("")
    lines += [f"- {n}" for n in notes]
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.ai.search_eval", description="Measure search quality per language.")
    p.add_argument("--split", choices=[*SPLITS, "all"], default="dev",
                   help="dev (default) for day-to-day comparisons; test once per tuning cycle; all runs both")
    p.add_argument("--queries-dir", type=Path, default=EVAL_DIR, help="where queries.<split>.jsonl live")
    p.add_argument("--rebuild", action="store_true", help="re-seed the evaluation database even if it is current")
    p.add_argument("--no-store", action="store_true", help="do not store the run in model_runs")
    p.add_argument("--publish", action="store_true",
                   help=f"also write each split's figures to {PUBLISHED_PATH.relative_to(BACKEND_DIR)}, which the "
                        "deployed AI Lab shows; commit that file")
    p.add_argument("--output", type=Path, help="write the dev (or only) run's results to this JSON file")
    p.add_argument("--summary", type=Path, help="append a Markdown summary to this file (CI: $GITHUB_STEP_SUMMARY)")
    p.add_argument("--baseline", type=Path, help="a JSON file from --output on main; fail if dev nDCG@10 drops "
                                                 "by more than --max-drop against it")
    p.add_argument("--max-drop", type=float, default=MAX_DEV_DROP)
    p.add_argument("--keyword-engine", choices=KEYWORD_ENGINES, help="text or atlas_search (default: the setting)")
    p.add_argument("--vector-engine", choices=VECTOR_ENGINES, help="in_process or atlas_vector (default: the setting)")
    p.add_argument("--scale", type=int, help="copy the seed catalogue across synthetic shops up to this many active "
                                             "listings first (once per evaluation database; 50000 for city scale)")
    return p.parse_args(list(argv))


def atlas_search_rank_fusion(server_version: str) -> str:
    from app.ai.atlas_search import rank_fusion_problem

    return rank_fusion_problem(server_version) or "supported."


def main(argv: Sequence[str] = ()) -> int:
    args = parse_args(argv)
    if args.publish and args.no_store:
        print("--publish needs a stored run; drop --no-store.", file=sys.stderr)
        return 2
    if problem := eval_db_problem(environment=settings.environment, mongodb_uri=settings.mongodb_uri,
                                  db_name=settings.mongodb_db, production_db=settings.production_mongodb_db,
                                  protected_hosts=settings.protected_host_list):
        print(f"Search evaluation stopped; no database was touched. {problem}", file=sys.stderr)
        return 2
    splits = list(SPLITS) if args.split == "all" else [args.split]
    for split in splits:  # bad labels stop the run before anything is seeded
        load_queries(split_path(split, args.queries_dir))
    engines = EnginePair(args.keyword_engine or settings.search_keyword_engine,
                         args.vector_engine or settings.search_vector_engine)
    # This process only: the semantic index keeps listing_vectors in step while the vector engine is atlas_vector.
    settings.search_keyword_engine, settings.search_vector_engine = engines.keyword, engines.vector  # type: ignore[assignment]
    counter = install_command_counter()
    db = Database()
    snapshot = ensure_snapshot(db, rebuild=args.rebuild)
    notes, ok = [], True
    if args.scale:
        try:
            added = scale_listings(db, args.scale)
        except ValueError as exc:
            print(f"Search evaluation stopped. {exc}", file=sys.stderr)
            return 2
        notes.append(f"Scaled to {args.scale} active listings ({added} added in this run).")
    if engines == EnginePair("atlas_search", "atlas_vector"):
        version = db.mongo.client.server_info().get("version", "unknown")
        notes.append(f"Fusion inside the database ($rankFusion) not run: {atlas_search_rank_fusion(version)}")
    runs = [run_split(db, s, queries_dir=args.queries_dir, store=not args.no_store, snapshot=snapshot,
                      engines=engines, counter=counter) for s in splits]
    if "test" in splits:
        notes.append("The test split runs once per tuning cycle, on the final version of a change (backend/eval/README.md).")
    if args.baseline:
        dev = next((r for r in runs if r["split"] == "dev"), None)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.is_file() else None
        if dev is None:
            notes.append("No dev run to compare with the baseline.")
        else:
            ok, note = compare(dev, baseline, args.max_drop)
            notes.append(note)
    if any(q.synthetic for s in splits for q in load_queries(split_path(s, args.queries_dir))):
        notes.append("Synthetic example rows are included: these figures are not a quality measurement yet.")
    if args.publish:
        publish([r["stored"] for r in runs if r["stored"] is not None])
        notes.append(f"Published to {PUBLISHED_PATH.relative_to(BACKEND_DIR)}: commit it so the deployed AI Lab "
                     "shows these figures.")
    text = summary_markdown(runs, notes)
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(text)
    if args.output:
        out = next((r for r in runs if r["split"] == "dev"), runs[0])
        args.output.write_text(json.dumps({k: v for k, v in out.items() if k != "stored"}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
