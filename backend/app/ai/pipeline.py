"""Train / refresh every AI component in one call (used by the seed, the retrain cron job and the CLI).

    uv run python -m app.ai.pipeline          # train now (make retrain; the local database)
    uv run python -m app.ai.pipeline --cron   # the retrain cron job: one queued request or the nightly run

The API never imports this module: POST /api/ai/retrain only queues a request in retrain_requests,
and the Render cron job (render.yaml, nearshop-retrain-*) runs --cron every 30 minutes in its own
process, then exits (app/jobs.py, run_retrain_cron). The training libraries (scikit-learn, pandas,
gensim, mlxtend) are imported only once there is something to train, so a cron run with nothing to
do stays small and quick.

Training rebuilds the semantic search index in this process's memory only (and caches the embeddings
in text_embeddings). Each API process rebuilds its own index on its next search after a retrain has
succeeded (app/jobs.py, SearchIndexRefresher), from those cached embeddings.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from collections.abc import Sequence

from app.core.database import Database

log = logging.getLogger(__name__)


def train_all(db: Database) -> dict:
    # Imported on use: the training libraries load only in the process that trains.
    from app.ai import anomaly, demand, recommend, word2vec_ref
    from app.ai.semantic_index import index
    from app.models import ModelRun

    out: dict = {}

    t = time.time()
    index.mark_dirty()
    index.ensure(db)
    db.model_runs.insert(
        ModelRun(
            name="embeddings",
            algorithm=index.provider_name,
            n_samples=len(index.product_ids),
            metrics={"listings_indexed": len(index.product_ids), "dimensions": int(index.matrix.shape[1])},
            data_note="Sentence embeddings for every active listing (cached per unique text).",
            duration_ms=int((time.time() - t) * 1000),
        )
    )
    out["embeddings"] = {"provider": index.provider_name, "listings": len(index.product_ids)}

    stats = word2vec_ref.train(db)
    db.model_runs.insert(ModelRun(name="word2vec", algorithm="Word2Vec skip-gram (gensim)", n_samples=stats["sentences"],
                    metrics=stats, data_note="Academic reference model trained on the product corpus.",
                    duration_ms=stats["duration_ms"]))
    out["word2vec"] = stats

    for name, fn in (("demand", demand.train_and_forecast), ("recommendations", recommend.train),
                     ("anomalies", anomaly.detect)):
        run = fn(db)
        out[name] = run.metrics
        log.info("trained %s in %sms: %s", name, run.duration_ms, run.metrics)
    return out


def run_cron(db: Database) -> dict | None:
    """The retrain cron job's work: one queued request or the nightly run, recorded in retrain_requests."""
    from app.jobs import run_retrain_cron

    return run_retrain_cron(db, train_all)


def _stop_on_sigterm(signum, _frame) -> None:
    # SystemExit, not the default silent exit: run_retrain_cron records the stopped request as failed.
    raise SystemExit(f"stopped by signal {signum} (a deploy or the cron job's run time limit)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ai.pipeline", description=__doc__.split("\n")[0])
    parser.add_argument("--cron", action="store_true",
                        help="process one queued retrain request or the nightly run, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.cron:
        print(train_all(Database()))
        return 0
    from app.jobs import RetrainFailed

    previous = signal.signal(signal.SIGTERM, _stop_on_sigterm)
    try:
        done = run_cron(Database())
    except RetrainFailed as exc:  # recorded on the request by run_retrain_cron
        print(f"Retrain failed ({type(exc.__cause__).__name__}); recorded on retrain request {exc.request_id} "
              f"in retrain_requests.", file=sys.stderr)
        return 1
    except Exception as exc:  # outside the training: the database unreachable, most likely
        print(f"Retrain cron run failed outside the training ({type(exc).__name__}); its outcome is not "
              f"recorded; a request it had claimed stays running until the next cron run marks it failed.", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps({"request": done["id"], "kind": done["kind"], "status": done["status"],
                      "outcome": done["outcome"]} if done else {"request": None}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
