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

This module changes no ranking: it only calls run_search.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pymongo.errors import DuplicateKeyError

from app.core.config import BACKEND_DIR, settings
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
    return {
        "queries": len(outcomes),
        "scored_queries": len(scored),
        "ndcg@10": _mean([o.ndcg for o in scored]),
        "recall@20": _mean([o.recall for o in scored]),
        "mrr": _mean([o.rr for o in scored]),
        "zero_result_rate": _mean([1.0 if o.total == 0 else 0.0 for o in scored]),
        "latency_p95_ms": round(p95, 1) if p95 is not None else None,
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
    """Reuse the seeded evaluation database while its fingerprint matches, else re-seed it. Returns the fingerprint."""
    fingerprint = seed_fingerprint()
    meta = db.mongo[SNAPSHOT_COLLECTION]
    current = meta.find_one({"_id": "snapshot"})
    if current and current.get("fingerprint") == fingerprint and not rebuild:
        print(f"Reusing the evaluation database {db.mongo.name!r} (seeded {current.get('built_at')}).")
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
    meta.insert_one({"_id": "snapshot", "fingerprint": fingerprint, "seed_city": settings.seed_city,
                     "built_at": utcnow()})
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


def evaluate(db: Database, queries: Sequence[LabelledQuery]) -> list[QueryOutcome]:
    """Run every query through run_search (unlogged) and score it."""
    from app.ai.semantic_index import index
    from app.services.search import SearchParams, reset_vocabulary, run_search

    def params(q: LabelledQuery) -> SearchParams:
        return SearchParams(q=q.text, lat=q.lat, lng=q.lng, radius_km=q.radius_km, page_size=RECALL_K)

    index.mark_dirty()
    index.ensure(db)
    reset_vocabulary()
    if queries:  # warm-up, untimed: the vocabulary and caches load on the first search
        run_search(db, params(queries[0]), log=False)
    out = []
    for q in queries:
        started = time.perf_counter()
        res = run_search(db, params(q), log=False)
        latency = (time.perf_counter() - started) * 1000
        ranked = [g["catalog_item_id"] for g in res["groups"]]
        out.append(score_query(q, ranked, res["total"], latency, available_items(db, q)))
    return out


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
    run = ModelRun(
        name=RUN_NAME,
        algorithm=f"run_search, {index.provider_name or 'unknown'} embeddings",
        n_samples=len(queries),
        metrics={"split": split, "commit": commit, "commit_dirty": dirty, "queries_file": queries_file,
                 "synthetic_queries": synthetic, "snapshot": snapshot[:12], "embedding": index.provider_name,
                 **result},
        data_note=(f"{len(queries)} labelled {split} queries ({synthetic} synthetic examples) on the seeded "
                   f"evaluation snapshot."),
        uses_demo_data=True,
        duration_ms=duration_ms,
    )
    db.model_runs.insert(run)
    return run


def run_split(db: Database, split: str, *, queries_dir: Path = EVAL_DIR, store: bool = True,
              snapshot: str = "") -> dict[str, Any]:
    path = split_path(split, queries_dir)
    queries = load_queries(path)
    started = time.time()
    outcomes = evaluate(db, queries)
    result = aggregate(outcomes)
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
        "embedding": m.get("embedding"), "overall": m.get("overall") or {},
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
        lines += [f"**{run['split']} split** at {str(run['commit'])[:10]}{' (uncommitted changes)' if run.get('commit_dirty') else ''}",
                  "", "| Language | Queries | Scored | nDCG@10 | Recall@20 | MRR | Zero-result | p95 ms |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        rows = [("All", run["overall"]), *run["by_language"].items()]
        for name, m in rows:
            lines.append(f"| {name} | {m['queries']} | {m['scored_queries']} | {_fmt(m['ndcg@10'])} | "
                         f"{_fmt(m['recall@20'])} | {_fmt(m['mrr'])} | {_fmt(m['zero_result_rate'])} | "
                         f"{_fmt(m['latency_p95_ms'], 0)} |")
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
    return p.parse_args(list(argv))


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
    db = Database()
    snapshot = ensure_snapshot(db, rebuild=args.rebuild)
    runs = [run_split(db, s, queries_dir=args.queries_dir, store=not args.no_store, snapshot=snapshot) for s in splits]
    notes, ok = [], True
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
