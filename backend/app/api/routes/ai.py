"""AI Lab: model cards, retraining, and a side-by-side comparison of search methods.

Retraining never runs in the API process (it would keep scikit-learn, pandas, gensim and mlxtend in
the API's memory): POST /ai/retrain only queues a request, which the retrain cron job processes in
its own process (app/jobs.py, app/ai/pipeline.py --cron). Nothing here imports app.ai.pipeline."""
from fastapi import APIRouter, Query

from app import jobs
from app.ai import word2vec_ref
from app.ai.semantic_index import index
from app.core.deps import DB, AdminUser
from app.models import ModelRun
from app.schemas.serializers import iso
from app.services.loaders import with_category
from app.services.meta import city_config
from app.services.search import SearchParams, keyword_scores, run_search
from app.utils.text import normalize_query

router = APIRouter(prefix="/ai", tags=["ai"])

MODEL_INFO = {
    "embeddings": {
        "title": "Semantic search",
        "purpose": "Understands what a search means, so 'phone charging adapter' finds 'Samsung 25W USB-C charger'.",
    },
    "word2vec": {
        "title": "Word2Vec (reference)",
        "purpose": "Academic baseline: expands a query with words that co-occur in the product catalogue.",
    },
    "demand": {
        "title": "Demand prediction",
        "purpose": "Predicts next-7-day sales per listing to suggest restock quantities before items run out.",
    },
    "recommendations": {
        "title": "Frequently bought together",
        "purpose": "Market-basket analysis over completed purchases to suggest bundles.",
    },
    "anomalies": {
        "title": "Price anomaly detection",
        "purpose": "Flags listings priced far from other shops selling the same item, for human review.",
    },
}


def _request_out(req: dict | None) -> dict | None:
    if req is None:
        return None
    return {**req, **{k: iso(req[k]) for k in ("requested_at", "started_at", "finished_at")}}


def _card_status(run: ModelRun | None, state: dict) -> str:
    """queued or training while a retrain request is; failed when the last finished request failed
    before this model was trained; otherwise trained or not_trained."""
    active, finished = state["active"], state["last_finished"]
    if active is not None:
        return "training" if active["status"] == "running" else "queued"
    if finished is not None and finished["status"] == "failed" and (
            run is None or (finished["started_at"] and run.created_at < finished["started_at"])):
        return "failed"
    return "trained" if run is not None else "not_trained"


@router.get("/models")
def models(db: DB):
    runs, state = db.gather(lambda: db.model_runs.find(sort=[("created_at", -1)], limit=200),
                            lambda: jobs.retrain_state(db))
    latest: dict[str, ModelRun] = {}
    history: dict[str, list] = {}
    for r in runs:
        latest.setdefault(r.name, r)
        history.setdefault(r.name, []).append({"at": iso(r.created_at), "n_samples": r.n_samples,
                                               "duration_ms": r.duration_ms})
    return {
        # Read from retrain_requests, whichever process trains. training is true only while a request is
        # running (the page polls every 3 s while it is); queued, while one waits for the cron job's next
        # run, up to jobs.CRON_EVERY_MINUTES minutes.
        "training": state["active"] is not None and state["active"]["status"] == "running",
        "queued": state["active"] is not None and state["active"]["status"] == "queued",
        "retrain": {"active": _request_out(state["active"]), "last_finished": _request_out(state["last_finished"]),
                    "daily_limit": jobs.RETRAIN_DAILY_LIMIT, "cron_every_minutes": jobs.CRON_EVERY_MINUTES},
        "models": [
            {"name": name, **info,
             "status": _card_status(latest.get(name), state),
             "algorithm": latest[name].algorithm if name in latest else None,
             "trained_at": iso(latest[name].created_at) if name in latest else None,
             "n_samples": latest[name].n_samples if name in latest else 0,
             "metrics": latest[name].metrics if name in latest else {},
             "note": latest[name].data_note if name in latest else "Not trained yet",
             "uses_demo_data": latest[name].uses_demo_data if name in latest else False,
             "duration_ms": latest[name].duration_ms if name in latest else 0,
             "runs": history.get(name, [])[:5]}
            for name, info in MODEL_INFO.items()
        ],
    }


@router.post("/retrain")
def retrain(user: AdminUser, db: DB):
    """Queue a retrain of every model and return at once. 409 while one is queued or running, 429 after
    jobs.RETRAIN_DAILY_LIMIT requests in 24 hours. started stays false: the training starts in the retrain
    cron job's next run, within jobs.CRON_EVERY_MINUTES minutes."""
    request = jobs.enqueue_retrain(db, user.id)
    return {"started": False, "queued": True, "request": _request_out(request),
            "message": f"Retrain queued. It starts within {jobs.CRON_EVERY_MINUTES} minutes and the model "
                       f"cards update when it finishes."}


@router.get("/search-compare")
def search_compare(db: DB, q: str = Query(..., min_length=2, max_length=120), limit: int = Query(6, ge=1, le=20)):
    """Same query through each method, over all listings (no location filter), top unique items."""
    norm = normalize_query(q)
    products = {p.id: p for p in with_category(
        db, db.products.find({"is_active": True}, projection={"description": 0, "specs": 0}))}

    def top_unique(scores: dict[int, float]) -> list[dict]:
        seen, out = set(), []
        for pid, score in sorted(scores.items(), key=lambda kv: -kv[1]):
            p = products.get(pid)
            if p is None:
                continue
            key = p.catalog_item_id or f"p{p.id}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": p.name, "icon": p.icon, "category": p.category.slug, "score": round(score, 3)})
            if len(out) >= limit:
                break
        return out

    kw = keyword_scores(db, norm.split())
    w2v_terms = word2vec_ref.expand(norm)
    w2v = keyword_scores(db, w2v_terms)
    sem = {k: v for k, v in index.query(db, norm).items() if v > 0.25}

    center = city_config()["center"]
    res = run_search(db, SearchParams(q=q, lat=center["lat"], lng=center["lng"], radius_km=50, page_size=limit),
                     log=False)
    hybrid_groups = [{"name": g["name"], "icon": g["icon"], "category": g["category"], "score": g["relevance"]}
                     for g in res["groups"]]

    return {
        "query": q,
        "normalized": norm,
        "methods": [
            {"key": "keyword", "title": "Keyword only (text index)",
             "note": "Exact words in the listing, scored by MongoDB's weighted text index.",
             "results": top_unique(kw)},
            {"key": "word2vec", "title": "Word2Vec expansion", "note": f"Expanded to: {', '.join(w2v_terms)}",
             "results": top_unique(w2v)},
            {"key": "semantic", "title": "Sentence embeddings", "note": index.provider_name,
             "results": top_unique(sem)},
            {"key": "hybrid", "title": "NearShop hybrid", "note": "60% meaning + 40% keywords, used in the app.",
             "results": hybrid_groups},
        ],
    }
