# Search ranking tuning report (P5.5)

Ranking config in use: **r1** (NEARSHOP_SEARCH_RANKING_VERSION, default r1). Plan section 8.6, checks 8.2,
8.3 and 8.5.

## Status: no tuned config adopted yet

The tuning cycle has not run on real labels. The labelled set (B15, due 10 Jan 2027) has not arrived:
backend/eval holds only the 10 synthetic example rows from P5.1 (6 dev, 4 test), and the stored baseline
(backend/eval/published.json) was measured on them. P5.5 needs 300 or more labelled queries and a baseline
on them, so the tuning tool refuses to run without them (it stops before touching any database).

This change ships the machinery, so the cycle can run as soon as the labels arrive:

- every ranking constant and group weight in a versioned config, RankingConfig in backend/app/core/config.py;
  r1 holds exactly the hand-set values search used before (a snapshot test checks that r1 reproduces the
  old ranking on the test database);
- the tuning harness, backend/app/ai/search_tuning.py: a random search over 200 or more configs on the dev
  split, then the chosen config once on the test split, with the adoption rule;
- suggestion hygiene (below), which needs no labels and is in force now.

## The r1 config

| Field | r1 | What it does | Search range |
|---|---:|---|---|
| sem_floor | 0.22 | cosine similarity mapped to 0 | 0.10 to 0.35 |
| sem_ceil | 0.62 | cosine similarity mapped to 1 | 0.50 to 0.80 (at least 0.15 above sem_floor) |
| sem_min_match | 0.34 | least similarity for a listing with no keyword hit | 0.25 to 0.45 |
| w_semantic | 0.6 | weight of semantic similarity in relevance | 0.30 to 0.85 |
| w_keyword | 0.4 | weight of the keyword score in relevance | 1 - w_semantic |
| relative_cutoff | 0.42 | drop listings below this share of the best relevance | 0.25 to 0.60 |
| w_local_term | 6.0 | keyword bonus per matched local term (P5.2) | 2 to 10 |
| w_relevance | 0.75 | group score: relevance | 0.60 to 0.90 |
| w_proximity | 0.15 | group score: nearness | 0.05 to 0.25 |
| w_in_stock | 0.10 | group score: some offer in stock | 1 - w_relevance - w_proximity (0.02 or more) |
| out_of_stock_factor | 0.6 | group score multiplier when nothing is in stock | 0.40 to 0.90 |

## How a tuning cycle runs

1. Replace the example rows with the labelled set (backend/eval/README.md) and store the baseline:
   make eval EVAL_SPLIT=all EVAL_ARGS=--publish, and commit backend/eval/published.json.
2. Tune on the dev split, with the environment make eval uses (local MongoDB, database nearshop_eval):
   uv run python -m app.ai.search_tuning --test-once --output tuning.json, from backend/.
   - Objective: overall nDCG@10 on the dev split.
   - Constraints against r1 on the dev split: zero-result rate no higher, Recall@20 no lower.
   - r1 is itself a candidate, so the chosen config is never worse than r1 on the dev split.
   - It prints the top 5, then runs r1 and the chosen config once on the test split.
3. Adoption rule (plan section 8.6): adopt only if test nDCG@10 rises by 0.02 or more and the test
   zero-result rate does not rise. Run the test split once per cycle; never tune on it.
4. If adopted: add the chosen values to RANKING_CONFIGS under the next version (r2), set
   NEARSHOP_SEARCH_RANKING_VERSION=r2 on staging, check it there, then on production. Record the cycle
   below: date, commit, seed, the top 5, the test results per language and the decision.

## Cycle log

### Trial run on the synthetic example rows, 21 Oct 2026 (not a measurement)

Run with --allow-examples, which exercises the harness on the example rows and is never adoptable. Local
evaluation database seeded from the deterministic Ballari seed, all-MiniLM-L6-v2 embeddings, seed 0,
200 configs plus r1. It took about 35 seconds including the seed (the fetch stage of each query is computed
once and re-ranked under every config).

Dev split (6 synthetic rows), top 5:

| Rank | Version | nDCG@10 | Recall@20 | Zero-result |
|---|---|---:|---:|---:|
| 1 | cand-555847c3 | 0.9073 | 1.0000 | 0.0000 |
| 2 | cand-80148d3d | 0.9073 | 1.0000 | 0.0000 |
| 3 | cand-34841104 | 0.9055 | 1.0000 | 0.0000 |
| 4 | cand-64e32e35 | 0.9051 | 1.0000 | 0.0000 |
| 5 | cand-5ccecc58 | 0.9051 | 1.0000 | 0.0000 |

r1 on the same split: nDCG@10 0.8588, Recall@20 0.8889, zero-result rate 0. 174 of 201 configs met the
constraints. Test split (4 synthetic rows), once: r1 nDCG@10 0.8106, Recall@20 0.90; cand-555847c3 0.8396
and 1.00, zero-result rate 0 for both. The rule alone would adopt (+0.029), but **nothing is adopted**:
with one query per language, a difference of this size is noise. r1 stays.

Test split per language for the chosen config (synthetic): en 0.810, te 0.631, hi 0.917, misspelt 1.000
nDCG@10; Recall@20 1.000 for each. The test split has no Kannada row, so check 8.4 cannot be read from it.

### First real cycle (after the B15 labelled set arrives)

Not run. To be recorded here: test nDCG@10 and Recall@20 overall (check 8.2: 0.70 and 0.85) and, if
short, the shortfall per language; Kannada-subset test nDCG@10 (check 8.4: 0.60); the labelled zero-result
rate (3% or less); the decision and the adopted version.

## Suggestion hygiene (in force from this change)

Search suggestions (/api/search/suggest) and popular searches (/api/search/discover) are built from
search_history, but a query is shown only when all of these hold:

- 5 or more distinct signed-in users searched it in the last 30 days and got results, counted by user_id,
  never by session, so one person (or anonymous traffic) cannot reach the threshold alone;
- it is 60 characters or fewer;
- it passes the blocklist: no phone number (7 or more digits joined by dashes, dots or brackets, or 10 or
  more space-separated digits starting with 0 or 6 to 9), no email address or mail-provider domain (a bare
  provider name such as "outlook diary" is fine), no link (a scheme, www., t.me or wa.me, a domain with a
  path, or a spam-prone domain; a plain shop domain such as "amazon.in" is fine), and no abusive word (English, Hindi, Kannada and Telugu, in Latin letters and in their own scripts).

Settings: NEARSHOP_SUGGESTION_MIN_USERS (5, and never lower), NEARSHOP_SUGGESTION_WINDOW_DAYS (30),
NEARSHOP_SUGGESTION_MAX_CHARS (60) and NEARSHOP_SUGGESTION_BLOCKLIST (extra comma-separated words, no deploy
needed). The admin analytics view of search_history is unchanged: it is not public.

At launch traffic few queries reach 5 distinct users, so suggestions will show mostly catalogue items and
categories until usage grows. That is intended.
