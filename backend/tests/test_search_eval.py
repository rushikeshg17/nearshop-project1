"""Search evaluation harness (P5.1): metric arithmetic, label parsing, the per-language breakdown, a run
over the test database stored in model_runs, and the AI Lab's endpoint."""
import json
import math

import pytest

from app.ai import search_eval as se
from tests.conftest import CENTER, HEADERS

LOCAL = "mongodb://127.0.0.1:27017/?directConnection=true"


def query(qid="q1", lang="en", judgments=None, **kw):
    return se.LabelledQuery(query_id=qid, text=kw.get("text", "phone charger"), lang=lang, lat=CENTER[0],
                            lng=CENTER[1], radius_km=5, judgments=judgments or {1: 3})


def outcome(lang, relevant, total, latency=10.0, ndcg=1.0):
    return se.QueryOutcome(query_id=f"{lang}-{total}-{relevant}", lang=lang, has_relevant_in_radius=relevant,
                           total=total, latency_ms=latency, ndcg=ndcg, recall=ndcg, rr=ndcg)


# ---------------------------------------------------------------- metrics

def test_perfect_ranking_has_ndcg_one():
    judgments = {10: 3, 11: 2, 12: 2, 13: 1, 14: 0}
    assert se.ndcg_at_k([10, 11, 12, 13, 14], judgments) == pytest.approx(1.0)
    # Ties in grade may come in either order; extra unjudged results after the judged ones change nothing.
    assert se.ndcg_at_k([10, 12, 11, 13, 99, None], judgments) == pytest.approx(1.0)


def test_hand_computed_toy_case():
    judgments = {1: 3, 2: 2, 3: 0, 4: 1}
    ranked = [3, 1, 4, 2]
    # Gains 2^g - 1: item 3 -> 0, 1 -> 7, 4 -> 1, 2 -> 3, discounted by log2(rank + 1).
    dcg = 0 / math.log2(2) + 7 / math.log2(3) + 1 / math.log2(4) + 3 / math.log2(5)
    ideal = 7 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4) + 0 / math.log2(5)
    assert se.ndcg_at_k(ranked, judgments) == pytest.approx(dcg / ideal)
    assert se.ndcg_at_k(ranked, judgments) == pytest.approx(0.6610, abs=1e-4)
    # Relevant means grade 2 or more: items 1 and 2.
    assert se.recall_at_k(ranked, judgments) == 1.0
    assert se.recall_at_k(ranked, judgments, k=2) == 0.5
    assert se.reciprocal_rank(ranked, judgments) == 0.5
    # The cut-off: nDCG@2 only sees items 3 and 1, against the ideal 1 then 2.
    assert se.ndcg_at_k(ranked, judgments, k=2) == pytest.approx((7 / math.log2(3)) / (7 + 3 / math.log2(3)))
    # Only judged items some nearby shop lists count towards the ideal and the recall denominator.
    assert se.ndcg_at_k([1], judgments, candidates={1, 3}) == pytest.approx(1.0)
    assert se.recall_at_k([1], judgments, candidates={1, 3}) == 1.0


def test_score_query_marks_queries_without_relevant_item_in_radius():
    q = query(judgments={1: 3, 2: 1})
    assert se.score_query(q, [1], 1, 5.0, available={1, 2}).has_relevant_in_radius
    far = se.score_query(q, [], 0, 5.0, available={2})  # only the related item is listed nearby
    assert not far.has_relevant_in_radius and far.ndcg == far.recall == far.rr == 0.0


def test_zero_result_rate_ignores_queries_without_relevant_item_in_radius():
    outcomes = [outcome("en", True, 0), outcome("en", True, 4), outcome("en", False, 0), outcome("en", False, 0)]
    summary = se.summarise(outcomes)
    assert summary["queries"] == 4 and summary["scored_queries"] == 2
    assert summary["zero_result_rate"] == 0.5
    assert se.summarise([outcome("en", False, 0)])["zero_result_rate"] is None


def test_results_are_grouped_by_language():
    outcomes = [outcome("kn", True, 3, ndcg=0.5), outcome("en", True, 2, ndcg=1.0), outcome("kn", True, 0, ndcg=0.0),
                outcome("misspelt", False, 0)]
    result = se.aggregate(outcomes)
    assert list(result["by_language"]) == ["en", "kn", "misspelt"]  # in LANGUAGES order
    assert result["by_language"]["kn"]["queries"] == 2
    assert result["by_language"]["kn"]["ndcg@10"] == 0.25
    assert result["by_language"]["kn"]["zero_result_rate"] == 0.5
    assert result["by_language"]["en"]["ndcg@10"] == 1.0
    assert result["by_language"]["misspelt"]["scored_queries"] == 0
    assert result["overall"]["queries"] == 4 and result["overall"]["ndcg@10"] == 0.5


def test_latency_p95_is_nearest_rank_over_every_query():
    outcomes = [outcome("en", n % 2 == 0, 1, latency=float(n)) for n in range(1, 21)]
    assert se.summarise(outcomes)["latency_p95_ms"] == 19.0
    assert se.percentile([]) is None


def test_compare_fails_only_beyond_the_allowed_drop():
    base = {"overall": {"ndcg@10": 0.70}}
    assert se.compare({"overall": {"ndcg@10": 0.69}}, base)[0]
    assert se.compare({"overall": {"ndcg@10": 0.68}}, base)[0]  # exactly 0.02 lower passes
    assert not se.compare({"overall": {"ndcg@10": 0.67}}, base)[0]
    assert se.compare({"overall": {"ndcg@10": 0.1}}, None)[0]  # no main run yet


# ---------------------------------------------------------------- labels

def test_shipped_example_rows_parse_and_are_marked_synthetic():
    dev = se.load_queries(se.split_path("dev"))
    test = se.load_queries(se.split_path("test"))
    assert len(dev) + len(test) == 10
    assert all(q.synthetic and q.query_id.startswith("example-") for q in dev + test)
    assert {q.lang for q in dev + test} <= set(se.LANGUAGES)
    assert not {q.query_id for q in dev} & {q.query_id for q in test}


@pytest.mark.parametrize("row, error", [
    ({"lang": "fr"}, "lang"),
    ({"judgments": {"1": 4}}, "grade"),
    ({"judgments": {"abc": 1}}, "catalog item id"),
    ({"judgments": {}}, "no judgments"),
    ({"radius_km": 0}, "out of range"),
    ({"lat": True}, "wrong type"),
])
def test_bad_labels_are_refused(tmp_path, row, error):
    good = {"query_id": "x", "text": "tap", "lang": "en", "lat": 15.1, "lng": 76.9, "radius_km": 5,
            "judgments": {"1": 3}}
    path = tmp_path / "queries.dev.jsonl"
    path.write_text(json.dumps({**good, **row}) + "\n")
    with pytest.raises(se.LabelError, match=error):
        se.load_queries(path)
    path.write_text(json.dumps(good) + "\n\n" + json.dumps(good) + "\n")
    with pytest.raises(se.LabelError, match="repeats"):
        se.load_queries(path)


def test_evaluation_database_must_be_local_and_named_for_evaluation():
    def problem(**kw):
        args = {"environment": "development", "mongodb_uri": LOCAL, "db_name": "nearshop_eval",
                "production_db": "nearshop", "protected_hosts": []} | kw
        return se.eval_db_problem(**args)

    assert problem() is None
    assert "starts with" in problem(db_name="nearshop")
    assert "not local" in problem(mongodb_uri="mongodb+srv://cluster0.example.mongodb.net/")
    assert "evaluation runs only" in problem(environment="production")
    assert "evaluation runs only" in problem(environment="demo")


# ---------------------------------------------------------------- a run over the test database

def test_run_is_stored_per_split_and_shown_in_the_ai_lab(db, client, ids, tmp_path):
    charger_item = db.products.find_raw({"_id": ids["charger"]}, {"catalog_item_id": 1})[0]["catalog_item_id"]
    tape_item = db.products.find_raw({"_id": ids["tape"]}, {"catalog_item_id": 1})[0]["catalog_item_id"]
    rows = [
        {"query_id": "t1", "text": "phone charger", "lang": "en", "lat": CENTER[0], "lng": CENTER[1],
         "radius_km": 5, "judgments": {str(charger_item): 3, str(tape_item): 0}},
        # Nothing judged relevant is listed within 1 km of a point far from both shops: not scored.
        {"query_id": "t2", "text": "teflon tape", "lang": "kn-Latn", "lat": CENTER[0] - 0.3, "lng": CENTER[1],
         "radius_km": 1, "judgments": {str(tape_item): 3}},
    ]
    (tmp_path / "queries.dev.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    before = {r.id for r in db.model_runs.find({"name": "search_eval"})}
    try:
        result = se.run_split(db, "dev", queries_dir=tmp_path, snapshot="f" * 64)
        assert result["overall"]["queries"] == 2 and result["overall"]["scored_queries"] == 1
        assert result["by_language"]["en"]["ndcg@10"] == 1.0
        assert result["by_language"]["en"]["zero_result_rate"] == 0.0
        assert result["by_language"]["kn-Latn"]["scored_queries"] == 0

        stored = [r for r in db.model_runs.find({"name": "search_eval"}) if r.id not in before]
        assert len(stored) == 1
        assert stored[0].metrics["split"] == "dev" and stored[0].metrics["commit"]
        assert stored[0].metrics["overall"]["ndcg@10"] == 1.0
        assert db.search_history.count({"query": "phone charger"}) == 0  # evaluation searches are not logged

        body = client.get("/api/ai/search-eval", headers=HEADERS).json()
        assert body["splits"]["test"] is None
        assert body["splits"]["dev"]["overall"]["ndcg@10"] == 1.0
        assert body["splits"]["dev"]["by_language"]["kn-Latn"]["scored_queries"] == 0
        assert body["splits"]["dev"]["queries"] == 2
    finally:
        db.model_runs.delete_many({"name": "search_eval"})
