"""P5.5: the versioned ranking config, the tuning harness and suggestion hygiene."""
import os
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.ai import embeddings
from app.ai import search_tuning as tuning
from app.ai.semantic_index import index
from app.core.config import RANKING_CONFIGS, SEARCH_SIMILARITY_SCALE, RankingConfig, Settings, settings
from app.core.database import Database, utcnow
from app.core.database import client as mongo_client
from app.models import CatalogItem, Category, Product, Role, SearchEvent, Shop, User
from app.services import search
from app.services.loaders import reset_category_cache
from app.services.search import SearchParams, run_search
from database import schema
from tests.conftest import HEADERS

# ---------------------------------------------------------------- the default config reproduces today's ranking

C = (12.30, 76.65)
PROVIDER_STATE = ("_provider", "_provider_key", "_unfitted_fallback")  # app/ai/embeddings.py module state
# run_search's results on the dataset below before P5.5 (hand-set constants in app/services/search.py), captured
# with the code at the parent commit: [query, radius_km, total, did_you_mean, corrected_from, [[group key,
# relevance, score], ...]]. r1 must reproduce them exactly.
SNAPSHOT = [
    ["charger", 5, 2, None, None, [["c1", 1.0, 0.9333], ["p23", 0.94, 0.8886]]],
    ["fast charger", 8, 2, None, None, [["c1", 1.0, 0.9583], ["p23", 0.459, 0.5524]]],
    ["usb cable", 8, 2, None, None, [["c2", 0.995, 0.9878], ["c3", 1.0, 0.9062]]],
    ["teflon tape", 8, 1, None, None, [["c5", 1.0, 0.9917]]],
    ["tap leak", 8, 2, None, None, [["c5", 1.0, 0.9917], ["c6", 0.427, 0.5285]]],
    ["pipe", 8, 2, None, None, [["c7", 1.0, 0.9917], ["c8", 0.654, 0.7318]]],
    ["samsung", 8, 1, None, None, [["c1", 1.0, 0.9583]]],
    ["chargr", 8, 2, "charger", None, [["c1", 0.6, 0.6583], ["p23", 0.453, 0.5479]]],
    ["power bank", 8, 1, None, None, [["c4", 1.0, 0.9917]]],
    ["water tap", 3, 3, None, None, [["c6", 1.0, 0.8888], ["c5", 0.459, 0.5718], ["c7", 0.435, 0.5538]]],
    ["", 8, 9, None, None, [["c2", 1.0, 0.9917], ["c4", 1.0, 0.9917], ["c5", 1.0, 0.9917], ["c7", 1.0, 0.9917],
                            ["c8", 1.0, 0.9917], ["c1", 1.0, 0.9583], ["c6", 1.0, 0.9583], ["p23", 1.0, 0.9583],
                            ["c3", 1.0, 0.9062]]],
    ["cable", 3, 2, None, None, [["c2", 1.0, 0.9778], ["c3", 0.988, 0.4679]]],
]


@pytest.fixture(scope="module")
def rank_db(ids):
    """Its own database, with listings at four distances, some out of stock: enough for every weight to count."""
    name = os.environ["NEARSHOP_MONGODB_DB"] + "_rank"
    db = Database(name)
    schema.reset(db.mongo, confirm=name)
    mob = db.categories.insert(Category(slug="mobile", name="Mobile", icon="Smartphone", color_hue=1))
    plb = db.categories.insert(Category(slug="plumbing", name="Plumbing", icon="Droplets", color_hue=2, sort_order=1))
    spec = [("samsung-25w", "Samsung 25W USB-C Fast Charger", "Samsung", mob, ["phone charger", "fast charger", "adapter"]),
            ("mi-cable", "Mi USB Type-C Cable 1m", "Xiaomi", mob, ["usb cable", "charging cable", "data cable"]),
            ("boat-cable", "boAt Micro USB Cable", "boAt", mob, ["usb cable", "charger cable"]),
            ("ambrane-pb", "Ambrane 10000mAh Power Bank", "Ambrane", mob, ["power bank", "portable charger"]),
            ("tape", "Astral PTFE Thread Seal Tape", "Astral", plb, ["teflon tape", "leak", "tap leak"]),
            ("tap", "Jaquar Brass Bib Tap", "Jaquar", plb, ["water tap", "nal", "tap"]),
            ("pipe", "Supreme PVC Pipe 1 inch", "Supreme", plb, ["pipe", "water pipe"]),
            ("elbow", "Astral CPVC Elbow", "Astral", plb, ["pipe fitting", "elbow", "pipe"])]
    items = [db.catalog_items.insert(CatalogItem(slug=s, name=n, brand=b, category_id=c.id, subcategory="x",
                                                 typical_price=100, mrp=120, tags=t, icon="Box"))
             for s, n, b, c, t in spec]
    owners = db.users.insert_many([User(name=f"O{i}", email=f"o{i}@example.com", password_hash="x", role=Role.OWNER)
                                   for i in range(4)])
    offsets = [0.004, 0.02, 0.045, 0.06]
    shops = db.shops.insert_many([
        Shop(owner_id=o.id, name=f"S{i}", slug=f"s{i}", address_line="a", locality="l", city="Mysuru",
             lat=C[0] + offsets[i], lng=C[1], category_ids=[mob.id, plb.id]) for i, o in enumerate(owners)])
    rows = []
    for n, item in enumerate(items):
        for k, shop in enumerate(shops):
            if (n + k) % 3 == 2:
                continue
            rows.append(Product(shop_id=shop.id, catalog_item_id=item.id, category_id=item.category_id,
                                name=item.name, brand=item.brand, keywords=" ".join(item.tags), icon=item.icon,
                                price=100 + 7 * n + 3 * k, mrp=item.mrp, quantity=0 if (n * 7 + k) % 5 == 0 else 3))
    rows.append(Product(shop_id=shops[1].id, catalog_item_id=None, category_id=mob.id, name="Local Charger Repair Kit",
                        brand=None, keywords="charger repair", icon="Box", price=60, mrp=None, quantity=2))
    db.products.insert_many(rows)
    # The TF-IDF test embeddings are fitted on the corpus of the first index build in the process: fit a fresh
    # one on this dataset, as when the snapshot was taken, and give the session's own back afterwards.
    saved = {k: getattr(embeddings, k) for k in PROVIDER_STATE if hasattr(embeddings, k)}
    embeddings.reset_provider()
    index.mark_dirty()
    search.reset_vocabulary()
    reset_category_cache()
    yield db
    for k, v in saved.items():
        setattr(embeddings, k, v)
    index.mark_dirty()  # the session database's index is rebuilt on its next search
    search.reset_vocabulary()
    reset_category_cache()
    mongo_client.drop_database(name)


def _ranked(db, q, radius, **kw):
    res = run_search(db, SearchParams(q=q, lat=C[0], lng=C[1], radius_km=radius), log=False, **kw)
    return [q, radius, res["total"], res["did_you_mean"], res["corrected_from"],
            [[g["key"], g["relevance"], g["score"]] for g in res["groups"]]], res


def test_r1_holds_the_hand_set_values():
    r1 = RANKING_CONFIGS["r1"]
    assert settings.ranking == r1 and settings.search_ranking_version == "r1"
    assert (r1.sem_floor, r1.sem_ceil) == (SEARCH_SIMILARITY_SCALE.sem_floor, SEARCH_SIMILARITY_SCALE.sem_ceil)
    assert r1.tunables() == {"sem_floor": 0.22, "sem_ceil": 0.62, "sem_min_match": 0.34, "w_semantic": 0.6,
                             "w_keyword": 0.4, "relative_cutoff": 0.42, "w_local_term": 6.0, "w_relevance": 0.75,
                             "w_proximity": 0.15, "w_in_stock": 0.10, "out_of_stock_factor": 0.6}


def test_default_config_reproduces_todays_ranking(rank_db):
    for expected in SNAPSHOT:
        q, radius = expected[0], expected[1]
        got, res = _ranked(rank_db, q, radius)
        assert got == expected, q
        assert res["engine"]["ranking"] == "r1"
        assert _ranked(rank_db, q, radius, ranking=RANKING_CONFIGS["r1"])[0] == expected  # explicit r1 too
        assert _ranked(rank_db, q, radius, cache={})[0] == expected  # the tuning memo changes nothing


def test_another_config_changes_the_ranking(rank_db):
    from dataclasses import replace

    no_penalty = replace(RANKING_CONFIGS["r1"], version="t", out_of_stock_factor=1.0)
    got, res = _ranked(rank_db, "cable", 3, ranking=no_penalty)
    assert got != SNAPSHOT[-1] and res["engine"]["ranking"] == "t"
    # The memo is shared across configs: the second config re-ranks the same candidates.
    cache: dict = {}
    first = _ranked(rank_db, "cable", 3, ranking=no_penalty, cache=cache)[0]
    assert _ranked(rank_db, "cable", 3, cache=cache)[0] == SNAPSHOT[-1] and len(cache) == 1
    assert first == got


# ---------------------------------------------------------------- tuning on a toy evaluation

def _toy(cfg: RankingConfig) -> dict:
    """A toy evaluation: nDCG peaks at w_semantic 0.7 and relative_cutoff 0.3; a high sem_min_match loses
    results; a large proximity weight loses recall."""
    ndcg = 1 - (cfg.w_semantic - 0.7) ** 2 - (cfg.relative_cutoff - 0.3) ** 2
    zero = 0.0 if cfg.sem_min_match < 0.40 else 0.5
    recall = 0.8 if cfg.w_proximity < 0.20 else 0.5
    return {"overall": {"ndcg@10": ndcg, "recall@20": recall, "zero_result_rate": zero, "mrr": ndcg},
            "by_language": {}}


def test_tuning_picks_the_best_feasible_config():
    calls: list[RankingConfig] = []

    def evaluate(cfg):
        calls.append(cfg)
        return _toy(cfg)

    result = tuning.random_search(evaluate, RANKING_CONFIGS["r1"], n_configs=200, seed=7)
    assert len(calls) == 201 and len({c.version for c in calls}) == 201  # the baseline and 200 distinct configs
    feasible = [c for c in result.candidates if c.feasible]
    best = max(feasible, key=lambda c: c.ndcg)
    assert result.chosen.config == best.config and not result.chosen.is_baseline
    assert result.chosen.config.sem_min_match < 0.40 and result.chosen.config.w_proximity < 0.20
    assert result.chosen.ndcg > tuning.Candidate(RANKING_CONFIGS["r1"], _toy(RANKING_CONFIGS["r1"]), True).ndcg
    assert len(result.top) == 5 and [c.ndcg for c in result.top] == sorted((c.ndcg for c in result.top), reverse=True)
    # An infeasible config with a higher nDCG is never chosen.
    assert all(c.feasible for c in result.top)
    report = tuning.report_markdown(result)
    assert result.chosen.config.version in report and "200 configs" in report


def test_tuning_keeps_the_baseline_when_nothing_beats_it():
    def evaluate(cfg):
        best = cfg == RANKING_CONFIGS["r1"]
        return {"overall": {"ndcg@10": 0.9 if best else 0.5, "recall@20": 0.9, "zero_result_rate": 0.0}}

    result = tuning.random_search(evaluate, RANKING_CONFIGS["r1"], n_configs=200, seed=1)
    assert result.chosen.is_baseline and result.chosen.config.version == "r1"


def test_feasibility_constraints():
    base = {"overall": {"ndcg@10": 0.5, "recall@20": 0.8, "zero_result_rate": 0.05}}
    assert tuning.feasible({"overall": {"ndcg@10": 0.6, "recall@20": 0.8, "zero_result_rate": 0.05}}, base)
    assert not tuning.feasible({"overall": {"ndcg@10": 0.9, "recall@20": 0.79, "zero_result_rate": 0.0}}, base)
    assert not tuning.feasible({"overall": {"ndcg@10": 0.9, "recall@20": 0.9, "zero_result_rate": 0.06}}, base)


def test_sampled_configs_stay_in_range():
    rng = random.Random(3)
    for _ in range(500):
        cfg = tuning.sample_config(rng)
        assert cfg.version.startswith("cand-")
        for key, (lo, hi) in tuning.SEARCH_SPACE.items():
            assert lo <= getattr(cfg, key) <= hi
        assert abs(cfg.w_semantic + cfg.w_keyword - 1) < 1e-6
        assert abs(cfg.w_relevance + cfg.w_proximity + cfg.w_in_stock - 1) < 1e-6
        assert cfg.w_in_stock >= tuning.MIN_IN_STOCK_WEIGHT and cfg.sem_ceil - cfg.sem_floor >= tuning.MIN_SEM_RANGE


@pytest.mark.parametrize(("chosen", "adopt"), [
    ((0.62, 0.05), True),    # +0.02 exactly, zero-result rate unchanged
    ((0.615, 0.05), False),  # under +0.02
    ((0.70, 0.06), False),   # better nDCG but more zero-result queries
    ((0.70, 0.01), True),
])
def test_adoption_rule(chosen, adopt):
    base = {"overall": {"ndcg@10": 0.60, "zero_result_rate": 0.05}}
    ok, why = tuning.adoption(base, {"overall": {"ndcg@10": chosen[0], "zero_result_rate": chosen[1]}})
    assert ok is adopt and why.startswith("Adopt" if adopt else "Not adopted")


def test_tuning_refuses_a_small_or_synthetic_label_set():
    real = [SimpleNamespace(synthetic=False)] * 300
    example = [SimpleNamespace(synthetic=True)] * 10
    baseline = {"synthetic_queries": 0}
    assert tuning.labelled_set_problem(real[:200], real[200:], baseline) is None
    assert "299 real" in tuning.labelled_set_problem(real[:299], [], baseline)
    assert "10 rows" in tuning.labelled_set_problem(example[:6], example[6:], baseline)
    assert "No search_eval baseline" in tuning.labelled_set_problem(real, [], None)
    assert "synthetic rows" in tuning.labelled_set_problem(real, [], {"synthetic_queries": 6})


def test_tuning_cli_stops_on_the_shipped_example_rows(capsys, monkeypatch):
    from app.ai import search_eval

    # Outside the evaluation database, the CLI stops first.
    assert tuning.main([]) == 2
    assert "evaluation database" in capsys.readouterr().err
    # The committed label files hold only the synthetic examples: it stops before connecting.
    monkeypatch.setattr(search_eval, "eval_db_problem", lambda **_: None)
    monkeypatch.setattr("app.core.database.Database", lambda *a, **k: pytest.fail("connected"))
    assert tuning.main([]) == 2
    assert "0 real queries" in capsys.readouterr().err
    assert tuning.main(["--configs", "50"]) == 2


# ---------------------------------------------------------------- suggestion hygiene

@pytest.fixture
def history(db):
    """search_history rows written by the test; removed afterwards."""
    written: list = []

    def add(query: str, users, *, results: int = 3, days_ago: float = 1, times: int = 1):
        rows = [SearchEvent(user_id=u, query=query, normalized_query=query, results_count=results, lat=C[0],
                            lng=C[1]).model_copy(update={"created_at": utcnow() - timedelta(days=days_ago)})
                for u in users for _ in range(times)]
        written.extend(r.id for r in db.search_history.insert_many(rows))

    yield add
    db.search_history.delete_many({"_id": {"$in": written}})


def test_suggestion_needs_five_distinct_signed_in_users(db, history):
    history("zq drill bit", [101], times=40)  # one person, many times
    history("zq drill bit", [102, 103, 104])
    history("zq drill bit", [None] * 10)  # anonymous searches never count
    history("zq drill bit", [105], days_ago=31)  # outside the 30-day window
    history("zq drill bit", [106], results=0)  # found nothing
    assert search.suggestions(db, "zq dr")["queries"] == []
    assert "zq drill bit" not in search.popular_searches(db, limit=50)
    history("zq drill bit", [107])  # the fifth distinct user
    assert search.suggestions(db, "zq dr")["queries"] == ["zq drill bit"]
    assert "zq drill bit" in search.popular_searches(db, limit=50)


def test_suggestions_rank_by_distinct_users(db, history):
    history("zq hammer", range(201, 206), times=10)
    history("zq hacksaw", range(201, 208))
    assert search.suggestions(db, "zq ha")["queries"] == ["zq hacksaw", "zq hammer"]


@pytest.mark.parametrize("text", [
    "zq 98450 12345", "zq 9845012345", "zq call 080-2345-6789", "zq ravi gmail.com", "zq ravi@example.com",
    "zq www.cheap-deals.xyz", "zq https //t.me/x", "zq fucking idiot", "zq bastards", "zq madarchod",
    "zq ಸೂಳೆಮಗ", "zq లంజ", "zq " + "x" * 58,
])
def test_blocked_text_never_reaches_suggestions(db, history, text):
    history(text, range(301, 320))
    assert not search.suggestion_allowed(text)
    assert search.suggestions(db, "zq")["queries"] == []
    assert text not in search.popular_searches(db, limit=50)


@pytest.mark.parametrize("text", ["phone charger", "rapeseed oil", "10000mah power bank", "1/2 inch pipe",
                                  "dickies shirt", "ಮೊಬೈಲ್ ಚಾರ್ಜರ್", "पानी की टंकी", "x" * 60])
def test_ordinary_queries_pass_the_blocklist(text):
    assert search.suggestion_allowed(text)


def test_configured_blocklist_words(monkeypatch):
    assert search.suggestion_allowed("zq spamword offer")
    monkeypatch.setattr(settings, "suggestion_blocklist", "spamword, ಕೆಟ್ಟಪದ")
    assert not search.suggestion_allowed("zq spamword offer")
    assert not search.suggestion_allowed("ಇದು ಕೆಟ್ಟಪದ")


def test_discover_endpoint_applies_the_threshold(client, history):
    history("zq wall putty", range(401, 405))
    assert "zq wall putty" not in client.get("/api/search/discover", headers=HEADERS).json()["popular"]
    history("zq wall putty", [405])
    assert "zq wall putty" in client.get("/api/search/discover", headers=HEADERS).json()["popular"]


def test_settings_refuse_a_lower_threshold_or_unknown_ranking():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, suggestion_min_users=4)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, search_ranking_version="r9")
    assert Settings(_env_file=None).ranking.version == "r1"
