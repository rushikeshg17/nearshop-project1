"""Ranking parameter tuning (P5.5; plan section 8.6). Random search over the ranking config on the dev split.

    uv run python -m app.ai.search_tuning                    # 200 configs on the dev split; prints the top 5
    uv run python -m app.ai.search_tuning --test-once        # then the chosen config, once, on the test split

Run it with the same environment as make eval (the evaluation database on the local MongoDB; see
backend/eval/README.md): NEARSHOP_ENVIRONMENT=development, NEARSHOP_MONGODB_URI the local replica set and
NEARSHOP_MONGODB_DB=nearshop_eval.

Every candidate is a RankingConfig (app/core/config.py). The objective is overall nDCG@10 on the dev split,
under two constraints against the current config (the baseline) on the same split: the zero-result rate is
no higher and Recall@20 is no lower. The baseline itself is always a candidate, so the chosen config is
never worse than it on the dev split. The fetch stage of every query (listings, keyword and semantic scores)
is computed once and re-ranked under each config, so 200 configs cost little more than one evaluation.

The test split is run once, with --test-once, on the chosen config and the baseline. The chosen config is
adopted only if test nDCG@10 rises by 0.02 or more and the zero-result rate does not rise. Adopting means
adding it to RANKING_CONFIGS under the next version and setting NEARSHOP_SEARCH_RANKING_VERSION, with the
results in docs/search/tuning-report.md.

The tuning stops before touching any database unless the labelled set has 300 or more real (not synthetic)
queries and a search_eval baseline on real rows is stored. --allow-examples runs it on the synthetic example
rows anyway, to exercise the harness; such a run is never adoptable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.core.config import RankingConfig, settings

MIN_LABELLED_QUERIES = 300
MIN_CONFIGS = 200
TOP_N = 5
ADOPT_MIN_NDCG_GAIN = 0.02  # plan section 8.6
EPS = 1e-9

# Ranges the random search draws from, uniformly. w_keyword is 1 - w_semantic and w_in_stock is
# 1 - w_relevance - w_proximity, so relevance and group scores keep their 0..1 scale.
SEARCH_SPACE: dict[str, tuple[float, float]] = {
    "sem_floor": (0.10, 0.35),
    "sem_ceil": (0.50, 0.80),
    "sem_min_match": (0.25, 0.45),
    "w_semantic": (0.30, 0.85),
    "relative_cutoff": (0.25, 0.60),
    "w_local_term": (2.0, 10.0),
    "w_relevance": (0.60, 0.90),
    "w_proximity": (0.05, 0.25),
    "out_of_stock_factor": (0.40, 0.90),
}
MIN_SEM_RANGE = 0.15  # sem_ceil - sem_floor
MIN_IN_STOCK_WEIGHT = 0.02


# ---------------------------------------------------------------- the search (pure)

def config_version(cfg: RankingConfig, prefix: str = "cand") -> str:
    """A stable name for a candidate: a hash of its values."""
    body = json.dumps(cfg.tunables(), sort_keys=True)
    return f"{prefix}-{hashlib.sha256(body.encode()).hexdigest()[:8]}"


def sample_config(rng: random.Random) -> RankingConfig:
    """One random config from SEARCH_SPACE, rounded to 3 places, that satisfies the weight constraints."""
    while True:
        v = {k: round(rng.uniform(lo, hi), 3) for k, (lo, hi) in SEARCH_SPACE.items()}
        in_stock = round(1 - v["w_relevance"] - v["w_proximity"], 3)
        if v["sem_ceil"] - v["sem_floor"] < MIN_SEM_RANGE or in_stock < MIN_IN_STOCK_WEIGHT:
            continue
        cfg = RankingConfig(version="", w_keyword=round(1 - v["w_semantic"], 3), w_in_stock=in_stock, **v)
        return replace(cfg, version=config_version(cfg))


def _metric(metrics: Mapping[str, Any], key: str) -> float | None:
    value = (metrics.get("overall") or {}).get(key)
    return float(value) if value is not None else None


def feasible(metrics: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    """The constraints against the baseline (aggregate() results): zero-result rate no higher, Recall@20 no
    lower. A figure the baseline lacks is not constrained; one the candidate lacks fails."""
    for key, worse in (("zero_result_rate", lambda c, b: c > b + EPS), ("recall@20", lambda c, b: c < b - EPS)):
        base, cur = _metric(baseline, key), _metric(metrics, key)
        if base is None:
            continue
        if cur is None or worse(cur, base):
            return False
    return True


@dataclass
class Candidate:
    config: RankingConfig
    metrics: dict[str, Any]
    feasible: bool
    is_baseline: bool = False

    @property
    def ndcg(self) -> float:
        return _metric(self.metrics, "ndcg@10") or 0.0

    def row(self) -> dict[str, Any]:
        o = self.metrics.get("overall") or {}
        return {"version": self.config.version, "baseline": self.is_baseline, "feasible": self.feasible,
                "ndcg@10": o.get("ndcg@10"), "recall@20": o.get("recall@20"),
                "zero_result_rate": o.get("zero_result_rate"), "mrr": o.get("mrr"), **self.config.tunables()}


@dataclass
class TuningResult:
    baseline: Candidate
    candidates: list[Candidate]  # every config tried, best first (feasible ones before infeasible ones)
    seed: int
    n_configs: int
    top: list[Candidate] = field(default_factory=list)

    @property
    def chosen(self) -> Candidate:
        return self.top[0]


def _rank_key(c: Candidate) -> tuple:
    o = c.metrics.get("overall") or {}
    # Best nDCG@10 first; ties go to higher recall, a lower zero-result rate, and then the baseline.
    return (not c.feasible, -c.ndcg, -(o.get("recall@20") or 0.0), o.get("zero_result_rate") or 0.0,
            not c.is_baseline)


def random_search(evaluate: Callable[[RankingConfig], dict[str, Any]], baseline: RankingConfig, *,
                  n_configs: int = MIN_CONFIGS, seed: int = 0, top_n: int = TOP_N) -> TuningResult:
    """Evaluate the baseline and n_configs random configs with `evaluate` (a RankingConfig -> aggregate() result
    on the dev split) and rank them: feasible before infeasible, then by nDCG@10. The baseline is feasible by
    definition, so the chosen config (the first) never does worse than it on the dev split."""
    rng = random.Random(seed)
    base_metrics = evaluate(baseline)
    base = Candidate(baseline, base_metrics, True, is_baseline=True)
    tried = [base]
    seen = {json.dumps(baseline.tunables(), sort_keys=True)}
    while len(tried) < n_configs + 1:
        cfg = sample_config(rng)
        key = json.dumps(cfg.tunables(), sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        metrics = evaluate(cfg)
        tried.append(Candidate(cfg, metrics, feasible(metrics, base_metrics)))
    ranked = sorted(tried, key=_rank_key)
    return TuningResult(baseline=base, candidates=ranked, seed=seed, n_configs=n_configs,
                        top=[c for c in ranked if c.feasible][:top_n])


def adoption(test_baseline: Mapping[str, Any], test_chosen: Mapping[str, Any],
             min_gain: float = ADOPT_MIN_NDCG_GAIN) -> tuple[bool, str]:
    """The adoption rule on the single test-split run (plan section 8.6): nDCG@10 rises by min_gain or more and
    the zero-result rate does not rise."""
    nb, nc = _metric(test_baseline, "ndcg@10"), _metric(test_chosen, "ndcg@10")
    zb, zc = _metric(test_baseline, "zero_result_rate"), _metric(test_chosen, "zero_result_rate")
    if nb is None or nc is None:
        return False, "Not adopted: the test split has no scored query."
    gain = nc - nb
    if gain < min_gain - EPS:
        return False, f"Not adopted: test nDCG@10 {nc:.4f} against {nb:.4f} ({gain:+.4f}), under +{min_gain}."
    if zb is not None and (zc is None or zc > zb + EPS):
        return False, f"Not adopted: the test zero-result rate rose from {zb:.4f} to {zc}."
    return True, f"Adopt: test nDCG@10 {nc:.4f} against {nb:.4f} ({gain:+.4f}), zero-result rate not higher."


def labelled_set_problem(dev_queries: Sequence[Any], test_queries: Sequence[Any], stored_baseline: Mapping | None,
                         minimum: int = MIN_LABELLED_QUERIES) -> str | None:
    """Why tuning must not run yet (P5.5 working rules), or None: fewer than `minimum` real labelled queries
    across both splits, or no stored search_eval baseline measured on real rows."""
    real = sum(not q.synthetic for q in (*dev_queries, *test_queries))
    if real < minimum:
        return (f"The labelled set has {real} real queries ({len(dev_queries) + len(test_queries)} rows in all); "
                f"tuning needs {minimum} or more (B15).")
    if not stored_baseline:
        return "No search_eval baseline is stored for the dev split: run make eval on the labelled set first."
    if stored_baseline.get("synthetic_queries"):
        return "The stored dev baseline was measured on synthetic rows: run make eval on the labelled set first."
    return None


# ---------------------------------------------------------------- report

def _f(v: Any, digits: int = 4) -> str:
    return "-" if v is None else f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def report_markdown(result: TuningResult, test: Mapping[str, Any] | None = None, *, trial: bool = False) -> str:
    keys = list(SEARCH_SPACE) + ["w_keyword", "w_in_stock"]
    lines = [f"Random search: {result.n_configs} configs plus the baseline ({result.baseline.config.version}), "
             f"seed {result.seed}, on the dev split.", "",
             "| Rank | Version | nDCG@10 | Recall@20 | Zero-result | MRR | " + " | ".join(keys) + " |",
             "|---|---|---:|---:|---:|---:|" + "---:|" * len(keys)]
    for n, c in enumerate(result.top, 1):
        r = c.row()
        name = f"{r['version']} (baseline)" if c.is_baseline else r["version"]
        lines.append(f"| {n} | {name} | {_f(r['ndcg@10'])} | {_f(r['recall@20'])} | {_f(r['zero_result_rate'])} | "
                     f"{_f(r['mrr'])} | " + " | ".join(_f(r[k], 3) for k in keys) + " |")
    feasible_n = sum(c.feasible for c in result.candidates)
    b = result.baseline.row()
    lines += ["", f"Baseline {b['version']} on the dev split: nDCG@10 {_f(b['ndcg@10'])}, Recall@20 "
              f"{_f(b['recall@20'])}, zero-result rate {_f(b['zero_result_rate'])}, MRR {_f(b['mrr'])}.",
              "", f"{feasible_n} of {len(result.candidates)} configs met the constraints. Chosen: "
              f"{result.chosen.config.version}."]
    if test:
        lines += ["", "Test split, run once:", "",
                  "| Config | nDCG@10 | Recall@20 | Zero-result |", "|---|---:|---:|---:|"]
        for label in ("baseline", "chosen"):
            o = test[label].get("overall") or {}
            lines.append(f"| {label} | {_f(o.get('ndcg@10'))} | {_f(o.get('recall@20'))} | "
                         f"{_f(o.get('zero_result_rate'))} |")
        lines += ["", test["decision"], "", "Test split per language (chosen config; plan check 8.2 wants nDCG@10 "
                  "0.70 and Recall@20 0.85, check 8.4 Kannada nDCG@10 0.60):", "",
                  "| Language | Scored | nDCG@10 | Recall@20 | Zero-result |", "|---|---:|---:|---:|---:|"]
        for lang, m in (test["chosen"].get("by_language") or {}).items():
            lines.append(f"| {lang} | {m.get('scored_queries')} | {_f(m.get('ndcg@10'))} | {_f(m.get('recall@20'))} | "
                         f"{_f(m.get('zero_result_rate'))} |")
    if trial:
        lines += ["", "Trial on synthetic example rows (--allow-examples): not a measurement, never adoptable."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- running on the evaluation database

def tune(db: Any, dev: Sequence[Any], *, n_configs: int, seed: int) -> tuple[TuningResult, Callable]:
    from app.ai.search_eval import aggregate, evaluate

    cache: dict = {}
    available: dict[str, set[int]] = {}
    evaluate(db, dev, cache=cache, available=available)  # builds the index, warms up and fills the memo

    def run(cfg: RankingConfig, queries: Sequence[Any] = dev) -> dict[str, Any]:
        return aggregate(evaluate(db, queries, ranking=cfg, cache=cache, available=available, prepare=False))

    return random_search(run, settings.ranking, n_configs=n_configs, seed=seed), run


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.ai.search_tuning", description="Tune the ranking config.")
    p.add_argument("--configs", type=int, default=MIN_CONFIGS, help=f"random configs to try ({MIN_CONFIGS} or more)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--queries-dir", type=Path, default=None, help="where queries.<split>.jsonl live")
    p.add_argument("--test-once", action="store_true", help="run the chosen config and the baseline on the test split")
    p.add_argument("--allow-examples", action="store_true",
                   help="run on the synthetic example rows to exercise the harness; never adoptable")
    p.add_argument("--rebuild", action="store_true", help="re-seed the evaluation database first")
    p.add_argument("--output", type=Path, help="write every config's results to this JSON file")
    return p.parse_args(list(argv))


def main(argv: Sequence[str] = ()) -> int:
    from app.ai import search_eval as ev
    from app.core.database import Database

    args = parse_args(argv)
    if args.configs < MIN_CONFIGS:
        print(f"--configs must be {MIN_CONFIGS} or more.", file=sys.stderr)
        return 2
    if problem := ev.eval_db_problem(environment=settings.environment, mongodb_uri=settings.mongodb_uri,
                                     db_name=settings.mongodb_db, production_db=settings.production_mongodb_db,
                                     protected_hosts=settings.protected_host_list):
        print(f"Tuning stopped; no database was touched. {problem}", file=sys.stderr)
        return 2
    qdir = args.queries_dir or ev.EVAL_DIR
    dev, test = (ev.load_queries(ev.split_path(s, qdir)) for s in ev.SPLITS)
    if not args.allow_examples:
        stored = ev.load_published().get("dev")
        if problem := labelled_set_problem(dev, test, stored):
            print(f"Tuning stopped; no database was touched. {problem}", file=sys.stderr)
            return 2
    db = Database()
    ev.ensure_snapshot(db, rebuild=args.rebuild)
    result, run = tune(db, dev, n_configs=args.configs, seed=args.seed)
    test_out = None
    if args.test_once:
        test_out = {"baseline": run(result.baseline.config, test), "chosen": run(result.chosen.config, test)}
        if result.chosen.is_baseline:
            test_out["decision"] = "Not adopted: no config beat the baseline on the dev split."
        else:
            test_out["decision"] = adoption(test_out["baseline"], test_out["chosen"])[1]
        if args.allow_examples:
            test_out["decision"] = f"Trial on synthetic rows, not adoptable. The rule alone says: {test_out['decision']}"
    print(report_markdown(result, test_out, trial=args.allow_examples))
    if args.output:
        body = {"seed": args.seed, "configs": args.configs, "ranking_version": settings.ranking.version,
                "chosen": result.chosen.row(), "candidates": [c.row() for c in result.candidates],
                "by_language_chosen": result.chosen.metrics.get("by_language"),
                "test": test_out, "trial": args.allow_examples}
        args.output.write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
