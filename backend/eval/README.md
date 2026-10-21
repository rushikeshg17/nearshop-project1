# Search evaluation

Search quality as a number per language, measured the same way on every change (P5.1, plan section 8.6).
The harness is backend/app/ai/search_eval.py; it changes no ranking, it only calls run_search.

```
make eval                        # the dev split, on the local MongoDB (docker-compose.yml)
make eval EVAL_SPLIT=test        # the held-out test split: once per tuning cycle
make eval EVAL_SPLIT=all         # both
make eval EVAL_ARGS=--rebuild    # re-seed the evaluation database first
```

**The rows shipped here are 10 synthetic examples** (query ids starting `example-`, marked `"synthetic": true`),
written to exercise the harness. They are not a quality measurement. The field team (B15) supplies the real
labelled rows; replace the example rows when they arrive.

## Label format

Two files, `queries.dev.jsonl` and `queries.test.jsonl`, in UTF-8, one JSON object per line (blank lines are
skipped):

| Field | Type | Meaning |
|---|---|---|
| `query_id` | string | Unique across both files, e.g. `kn-0042`. |
| `text` | string | The query exactly as the customer typed or said it, in its own script. |
| `lang` | string | One of `en`, `kn`, `kn-Latn`, `te`, `te-Latn`, `hi`, `hi-Latn`, `misspelt`. `kn` is Kannada script, `kn-Latn` Kannada written in Latin letters, and likewise for Telugu and Hindi; `misspelt` is a misspelling or brand typo of any language. |
| `lat`, `lng` | number | Where the customer stood (WGS84 degrees). |
| `radius_km` | number | The search radius, greater than 0 (the app's default is 5). |
| `judgments` | object | Catalog item id (as a string key) to grade: `0` irrelevant, `1` related, `2` good substitute, `3` exact. Items not listed count as 0. |
| `synthetic` | boolean, optional | `true` only for made-up example rows. |
| `notes` | string, optional | Annotator notes; ignored by the harness. |

Example:

```
{"query_id": "kn-0042", "text": "ಇಸ್ತ್ರಿ ಪೆಟ್ಟಿಗೆ", "lang": "kn", "lat": 15.148, "lng": 76.923, "radius_km": 5, "judgments": {"11": 3, "12": 3, "13": 3}}
```

Catalog item ids are those of the evaluation database (below), which the deterministic seed builds from
backend/database/seed/data/catalog.json in file order, so the Nth item of the catalogue has id N. If the
catalogue file changes, check the ids of the labelled items before the next run.

## Metrics

Computed overall and per language, for each split:

- **nDCG@10**, graded: gain 2^grade − 1, discount log2(rank + 1). The ideal ranking is built from the judged items
  that some shop inside the query's radius lists.
- **Recall@20**: relevant items (grade 2 or more) in the top 20 results, over the relevant items in radius.
- **MRR**: 1 / rank of the first relevant item in the top 20 (0 when there is none).
- **Zero-result rate**: queries with no result, counted only over queries with a relevant item inside the radius.
- **Latency p95**: nearest-rank 95th percentile of run_search time, in milliseconds, over every query.

nDCG, recall, MRR and the zero-result rate count only *scored* queries, those with a relevant item (grade 2 or more)
listed by a shop inside the radius. A result is a group (one catalog item across shops), so each catalog item is
ranked once.

## Evaluation database and stored runs

Runs use a fixed snapshot so results are reproducible: a local MongoDB database named `nearshop_eval` (EVAL_DB),
built by the deterministic seed (city ballari) on the first run and reused while backend/database/seed/seed.py and
its data files are unchanged; a changed seed rebuilds it. The harness refuses any database whose name does not start
with `nearshop_eval`, any host that is not local and any environment other than development or test, so it never
reaches Atlas whatever backend/.env says.

Each run is stored in that database's `model_runs` collection as an entry named `search_eval`, with the split, the
git commit (and whether the tree had uncommitted changes), the query file, the embedding model and the figures. The AI
Lab (/ai) shows the latest run of each split in the database its API reads: to see local runs there, run the API with
NEARSHOP_MONGODB_URI set to the local server and NEARSHOP_MONGODB_DB=nearshop_eval. A rebuilt snapshot starts a new
run history.

Options (`uv run python -m app.ai.search_eval --help`): `--output FILE` writes the dev run's figures as JSON,
`--summary FILE` appends a Markdown table, `--baseline FILE` compares dev nDCG@10 with an earlier `--output` file and
exits 1 when it fell by more than `--max-drop` (default 0.02), `--no-store` skips model_runs.

## CI

The `search-eval` job in .github/workflows/ci.yml runs the dev split:

- on every push to main, and saves the figures as the main baseline (GitHub Actions cache);
- on a pull request that touches search, text, embeddings, semantic-index, the harness, these files or the seed. It
  writes the summary table to the job summary and fails when dev nDCG@10 drops by more than 0.02 against the latest
  main run. With no main run yet it passes and says so.

The test split is never run in CI.

## Collection protocol (B15)

Owner: the field team collects; the ML lead (R5) settles disagreements and keeps this file current.

**At least 300 queries**, collected at Ballari shop counters and from customers, in this composition:

| Group | `lang` | Queries |
|---|---|---|
| English and colloquial English | `en` | 120 |
| Kannada script | `kn` | 50 |
| Kannada in Latin script | `kn-Latn` | 40 |
| Telugu (either script) | `te`, `te-Latn` | 30 |
| Hindi (either script) | `hi`, `hi-Latn` | 30 |
| Misspellings and brand typos | `misspelt` | 30 |

That is 180 not in English (150 or more is required).

1. **Collect** each query with where it was asked (latitude, longitude) and a radius (5 km unless the customer
   said otherwise). Record the text exactly; do not correct spelling or script. Nothing that identifies a person.
2. **Label**: two annotators grade every query independently against the catalogue items listed near that point:
   0 irrelevant, 1 related, 2 good substitute, 3 exact.
3. **Agreement**: compute Cohen's kappa between the two annotators on the binary split "grade 2 or more" versus
   "below 2", over every judged (query, item) pair. It must be at least 0.6; below that, revise the guidelines and
   relabel. Record it below.
4. **Disagreements** are settled by the ML lead (R5); the settled grade is the one written to the files.
5. **Split** 60% dev and 40% test, stratified by language: within each `lang`, shuffle with a fixed seed and put 60%
   in `queries.dev.jsonl`, the rest in `queries.test.jsonl`. A query never appears in both.
6. **Use**: work is compared on the dev split. The test split runs once per tuning cycle, on the final version of a
   change, and decides adoption (plan section 8.6: a ranking change needs test nDCG@10 up by 0.02 or more and no rise
   in the zero-result rate).
7. **Baseline**: once the set is in, run `make eval EVAL_SPLIT=all` on main before any ranking change and record it.

## Record

| Item | Value |
|---|---|
| Labelled set delivered (B15) | not yet (due 10 Jan 2027); 10 synthetic example rows only |
| Queries (total / not in English) | – |
| Cohen's kappa (grade ≥ 2 vs < 2) | – |
| Baseline run, dev and test (commit, date) | – |
