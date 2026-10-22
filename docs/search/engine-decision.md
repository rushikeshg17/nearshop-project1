# Search engine decision record (P5.7)

Should NearShop keep its search engines, mix them, or switch to MongoDB Atlas Search and Atlas Vector Search?
Plan section 8.6 (engine decision), sections 8.10 and 11 (storage, tiers and cost); checks 8.2 and 8.4, with
4.4 and 5.4 kept passing.

## Status: no candidate measured yet, so the record says keep

**Recommendation: keep** today's engines (the products text index and the in-process vector index). The
decision rule (item 11 below) recommends a hybrid or a switch only for a candidate that passes it. No Atlas
candidate has been measured yet, so none has passed, and the rule's default applies. This is provisional. It is
replaced once the evaluation cycle below has run on the labelled set and the evaluation cluster.

The measurements are missing for three reasons. None of them can be fixed in code:

1. **No labelled query set.** backend/eval still holds only the 10 synthetic example rows from P5.1 (6 dev,
   4 test). The B15 field labels have not arrived. Any comparison on 6 dev rows is noise (see
   docs/search/tuning-report.md).
2. **No evaluation cluster.** Plan step 5-A (a temporary Flex cluster in its own Atlas project) is due by
   19 Apr 2027 and has not been created. MongoDB's Atlas local deployment, the Docker image that runs search
   and vector queries locally, was not available on the development machine either (no Docker).
3. **The P1.1 guard refuses the evaluation cluster as step 5-A describes it.** Step 5-A builds the evaluation
   database "under the test environment". But database/schema.py check_reset_allowed lets reset and the seed
   run in development and test only against a local host (localhost, 127.0.0.1, ::1 or mongo). A hosted Flex
   cluster under NEARSHOP_ENVIRONMENT=test is refused, and so is the --scale option, which uses the same guard.
   As the prompt requires, the guard was not weakened. The team chooses one of these (R2 and R5):
   - run the Atlas candidates on MongoDB's Atlas local deployment on localhost, which the guard allows. That
     measures quality, operations per search and memory. It does not measure Flex latency or Flex storage;
   - or build the evaluation cluster under NEARSHOP_ENVIRONMENT=demo, which the guard allows for a hosted
     cluster. In that case app/ai/search_eval.py (EVAL_ENVIRONMENTS: development and test, local hosts only)
     needs a reviewed change of its own before it will run there.

What this change ships, so that the cycle can run as soon as those are in place:

- the engine settings (backend/app/core/config.py):
  - NEARSHOP_SEARCH_KEYWORD_ENGINE: text or atlas_search, default text;
  - NEARSHOP_SEARCH_VECTOR_ENGINE: in_process or atlas_vector, default in_process;
  - NEARSHOP_SEARCH_SHADOW_ENGINES: a keyword,vector pair for P5.6 shadow mode, empty for off.

  The API refuses to start in staging or production with a serving engine other than its default;
- the three index definitions and the queries (backend/app/ai/atlas_search.py);
- the listing_vectors and search_synonyms collections (backend/database/schema.py), kept in step only while an
  Atlas engine is configured. listing_vectors catches up with the listings before each Atlas Vector Search query
  (app/ai/semantic_index.py, ListingVectorSync: the in-process index's version check, watermark and nightly full
  pass, without a matrix), and at startup when the engines use it. It never takes the vectors of a TF-IDF stand-in
  for a model that failed to load. The synonym service regenerates search_synonyms on every save;
- search sends each stage to its engine. A shadow engine pair runs after the response, on the P5.6 sampled
  share, and its top 20 is stored under the P5.6 label, for example "r1 + atlas-search + <vector engine>". Only
  the candidate is fetched again: production is re-ranked from the customer's own fetch and timed with the time
  that fetch took in the request;
- the operator commands: uv run python -m database.admin_tasks search-index plan|create|status|drop;
- the harness options: --keyword-engine, --vector-engine and --scale, plus the new measurements (below);
- tests/test_search_engines.py. With default settings, results equal those before P5.7 (a snapshot) and no
  Atlas query is sent.

## The candidates (item 9)

| # | Keyword stage | Vector stage | Fusion | Harness flags |
|---|---|---|---|---|
| 1 | text index | in-process | NearShop | (defaults) |
| 2 | Atlas Search | in-process | NearShop | --keyword-engine atlas_search |
| 3 | text index | Atlas Vector Search | NearShop | --vector-engine atlas_vector |
| 4 | Atlas Search | Atlas Vector Search | NearShop | both flags |
| 5 | Atlas Search | Atlas Vector Search | inside the database ($rankFusion) | not built: see below |

Candidates 3 and 4 run twice, once with each vector index: without quantization, and with scalar quantization
(admin_tasks search-index create --quantization scalar, after a drop).

**Candidate 5 cannot run in this design.** $rankFusion needs MongoDB 8.1 or later, and the local replica set
runs 8.2.2. But every input pipeline of $rankFusion reads the collection the aggregation runs on. The keyword
index is on products and the vector index on listing_vectors, as the prompt specifies. Fusing inside the
database would need the vectors stored on products: about 1.5 KB more per listing, and the products index would
then carry the vector field. The harness prints this reason whenever both Atlas engines run
(atlas_search.rank_fusion_problem). If candidate 4 comes close to the rule, a follow-up can trial vectors on
products.

## Measurements (item 8)

Measured on 22 Oct 2026:

- environment: a local MongoDB 8.2.2 single-node replica set on an Apple-silicon laptop;
- data: the deterministic Ballari seed (1,351 active listings), and the same seed scaled to 50,000 active listings
  with --scale 50000 (48,649 listings copied into synthetic shops near the shops they copy);
- model and ranking: all-MiniLM-L6-v2 embeddings and ranking config r1;
- queries: the dev split, which is still the 6 synthetic rows, so **these are not a quality measurement**;
- runs: each figure is from a second run on the reused snapshot, so memory does not include seeding.

Database operations are counted by a command listener: every command run_search sends, including getMore, but
not handshakes or pings. Memory is the evaluation process's resident set, which holds what an API process on the
same engines holds: the model and the vocabulary, and the vector matrix only for candidates whose vector stage is
in-process. With --vector-engine atlas_vector the harness brings listing_vectors in step instead and never builds
the matrix, as an API process on that engine does not (its startup warm-up skips it too).

### Candidate 1: today's engines

| Size | nDCG@10 | Recall@20 | MRR | Zero-result | Autocomplete hit rate | Search p95 | Ops per search | RSS after load / peak | Storage (no search indexes) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,351 listings | 0.859 | 0.889 | 0.889 | 0.000 | 0.333 (12 prefixes) | 27 ms | 6.3 | 281 / 282 MB | 55.6 MB |
| 50,000 listings | 0.870 | 0.889 | 0.889 | 0.000 | 0.333 (12 prefixes) | 394 ms | 9.3 | 472 / 618 MB | 112.7 MB |

The dev nDCG@10 at 1,351 listings, 0.8588, equals the P5.5 r1 figure. The new code paths leave today's ranking
unchanged. A suggest call costs 2 operations at both sizes.

Per language. One synthetic row each; the misspelt subset is example-dev-06.

| Language | nDCG@10 at 1,351 / 50,000 | Recall@20 | MRR | Zero-result | Autocomplete | p95 ms at 1,351 / 50,000 | Ops at 1,351 / 50,000 |
|---|---:|---:|---:|---:|---:|---:|---:|
| en | 0.976 / 0.976 | 1.000 | 1.000 | 0 | 0.0 | 11 / 394 | 7 / 9 |
| kn | 0.982 / 0.973 | 1.000 | 1.000 | 0 | 0.0 | 27 / 346 | 6 / 9 |
| kn-Latn | 1.000 / 1.000 | 1.000 | 1.000 | 0 | 0.0 | 9 / 315 | 6 / 10 |
| te-Latn | 0.922 / 1.000 | 1.000 | 1.000 | 0 | 1.0 | 12 / 334 | 7 / 9 |
| hi-Latn | 0.944 / 0.944 | 1.000 | 1.000 | 0 | 0.0 | 13 / 362 | 6 / 10 |
| misspelt | 0.329 / 0.329 | 0.333 | 0.333 | 0 | 1.0 | 17 / 328 | 6 / 9 |

What the 50,000-listing run shows, even on synthetic rows:

- Search p95 grows from tens of milliseconds to about 400 ms. That is inside the 600 ms target (check 4.4), but
  on a laptop next to its database. Operations per search rise from about 6 to about 9, because the listing
  fetch and the text-index result need getMore batches.
- Both of today's stages work city-wide. The text index scores every matching listing in the city, and the
  in-process index scores every active listing (a 50,000 by 384 float32 matrix, about 77 MB in each API
  process). Only then does search keep the listings of the shops in radius. P8.2 has to scope this.

### Candidates 2 to 4

| Size | Candidate | Every item 8 measurement |
|---|---|---|
| 1,351 and 50,000 | 2: Atlas Search + in-process | not measured (see Status) |
| 1,351 and 50,000 | 3: text + Atlas Vector Search, no quantization / scalar | not measured |
| 1,351 and 50,000 | 4: both Atlas engines, no quantization / scalar | not measured |
| 1,351 and 50,000 | 5: fused inside the database | not buildable (see above) |

**Test split:** not run. It runs once, for the candidate with the best dev nDCG@10, after the candidates have been
measured (plan section 8.6). Running it now on today's engines would spend that one run on no decision.

## Cost, limits, storage and memory (item 10)

**Monthly cost.** Atlas Flex is billed by operations band: $8 a month at 0 to 100 operations per second, up to
$30 at 400 to 500 (plan section 11).

At the P2.5 load (30 search users), the load script makes about 150 searches and 450 suggest calls a minute,
that is 2.5 searches and 7.5 suggest calls a second. Search and suggestions then send:

- with today's engines at 1,351 listings: 2.5 × 6.3 + 7.5 × 2 = **about 31 operations a second**;
- at 50,000 listings: 2.5 × 9.3 + 7.5 × 2 = **about 38 operations a second**.

Both are inside the $8 band. A shadow engine pair adds, per sampled search, one fetch on the candidate's engines
only (production is re-ranked from the customer's fetch): at the P2.5 load and a 10% share, about 0.25 × the
candidate's operations per search, under 3 operations a second at today's counts. These figures exclude product pages, owner polling and reservations, and the P2.5
run itself has not happened yet (docs/perf/baseline.md). M10 at about 500 shops costs $56.94 a month (plan
section 11), with no Search Nodes in any candidate. On Flex and M10 the search process shares the cluster's
nodes, and the pricing page shows no separate charge for it. Confirm that on the first Atlas bill after an index
is created.

How each Atlas candidate is expected to change the operation count, to be confirmed by the harness:

- **Atlas Search** replaces the text-index find with one $search aggregation. All its hits come in the first
  batch (batchSize 2,000), so it never needs a getMore. The local-term stage still sends its own 2 queries.
- **Atlas Vector Search** adds one $vectorSearch aggregation per search. The in-process index adds none: it
  reads the database only when listings change. Unless something else falls, candidates 3 and 4 add an
  operation per search. The rule rejects a candidate whose operations per search rise.
- **Autocomplete** replaces the catalogue regex find with one $search. Suggest calls stay at 2 operations.

**Index count against each tier's limit.** The plan has 3 indexes. M0 (staging and demo) allows 3, so staging
can mirror production exactly. Flex (production) allows 10. Atlas counts these limits per cluster, so search-index
plan, status and create count the indexes of every database the operator's user can list, and create refuses to
take the cluster above the tier's limit or NearShop's 3, whichever is lower. Definition sizes (search-index plan) are all within
the 3 KB limit, and the plan has one synonym mapping in all:

| Index | Collection | Type | Definition size | Synonym mappings |
|---|---|---|---:|---:|
| nearshop_products_search | products | search | 1,257 bytes | 1 |
| nearshop_catalog_autocomplete | catalog_items | search | 256 bytes | 0 |
| nearshop_listing_vectors | listing_vectors | vectorSearch | 215 bytes (217 with scalar quantization) | 0 |

plan shows the vector index with the configured model's listed dimensions (384). create sizes it from the vectors
it has just written to listing_vectors, and writes none, and creates no index, when the configured model fails to
load on the operator's machine.

The synonym source holds one explicit mapping per active entry. P5.4 allows at most 500 entries, well under
Atlas's limit of 10,000 documents.

**Storage against the 3.5 GB alert.** The 70% alert on Flex's 5 GB is 3.5 GB (plan section 8.10). Today's data
and indexes are 55.6 MB at 1,351 listings and 112.7 MB at 50,000 (uncompressed documents plus B-tree indexes,
as the P2.2 storage card counts them).

listing_vectors would add about 1.6 KB per active listing: a 384-dimension float32 vector of 1,536 bytes, plus
ids, model and key. That is about 2 MB at 1,351 listings and about 80 MB at 50,000, before the vector index. The
Atlas search and vector index sizes are not reported to the driver. Read them in the Atlas UI after create and
add them here. Every candidate stays far below 3.5 GB on these estimates.

**Memory.** Today an API process holds the vector matrix: about 2 MB at 1,351 listings and 77 MB at 50,000. The
evaluation process measured 281 MB resident at 1,351 listings and 472 MB at 50,000, with a 618 MB peak. The
Standard instance has 2 GB, and the P2.5 target is 75% of it.

Atlas Vector Search (candidates 3 and 4) would drop the matrix from every API process: on atlas_vector neither the
startup warm-up nor search builds it, and the harness's memory figures for those candidates show that. Only the
AI routes and the owner analytics that query the matrix directly would still build it, on their first call. The
embedding model stays, to embed the query. On Flex and M10 the vector index then uses the cluster's own memory,
which the shared tier does not report.

## Recommendation (item 11)

The rule:

1. Choose the candidate with the best dev nDCG@10.
2. Recommend it only if all of these hold on its single test-split run:
   - overall nDCG@10 rises by 0.02 or more, and the zero-result rate does not rise;
   - English and Kannada subset nDCG@10 each drop by no more than 0.01;
   - checks 8.2 and 8.4 still pass: nDCG@10 0.70 or higher, Recall@20 0.85 or higher, Kannada subset 0.60 or
     higher;
   - search server p95 is no higher than today's at both sizes, and is 600 ms or less;
   - API peak memory and database operations per search are no higher;
   - it needs no Search Nodes and no tier change.
3. A recommended candidate then runs in shadow on production until the P5.6 guardrails pass.

**Today: keep.** No candidate has been measured, so none passes. Because the record says keep, no Atlas index is
created on staging or production. listing_vectors and search_synonyms stay empty there: the code writes them only
while an Atlas engine is configured, and migrate creates them as empty collections. search-index status must list
no search or vector index on staging or production. If any index has been created by then, search-index drop
removes the indexes and empties both collections.

## Stages scoped to nearby shops (P8.2, done)

With keep, P8.2 (backend/app/services/search.py) scoped the stages that worked city-wide:

- **The keyword stage.** The text-index query filters by the ids of the shops in radius. An Atlas Search
  candidate filters by shop_id inside the query.
- **The vector stage.** The in-process index scores only the listings fetched in radius, with the same
  similarities. An Atlas Vector Search candidate filters by shop_id.
- **The local-term stage.** local_term_scores matches only the listings of the shops in radius, in every
  candidate.

These stay city-wide:

- **The spelling vocabulary and the did-you-mean correction.** These are per process and city-wide, unchanged
  by any candidate.
- **Suggestions.** The catalogue match is city-wide by design, since the catalogue is shared, and so is the
  autocomplete index.

## How to run the cycle

1. Resolve the guard question (Status, point 3) and replace the example rows with the labelled set
   (backend/eval/README.md).
2. Create the evaluation cluster (step 5-A), or start the Atlas local deployment. Seed it with the harness:
   make eval, with the URI pointing there.
3. Check and build the indexes:
   - uv run python -m database.admin_tasks search-index plan --tier Flex
   - uv run python -m database.admin_tasks search-index create --tier Flex

   create fills search_synonyms and listing_vectors first. Run search-index status until all three indexes
   are READY and queryable.
4. On the dev split, for each candidate at 1,351 listings and again with --scale 50000 (a separate EVAL_DB):

   ```
   uv run python -m app.ai.search_eval --split dev --no-store --output c<N>.json \
     [--keyword-engine atlas_search] [--vector-engine atlas_vector] [--scale 50000]
   ```

   Then rebuild the vector index with --quantization scalar (drop, then create) and repeat candidates 3 and 4.
   Run each figure on a second run, so that memory is measured without seeding.
5. Pick the best dev nDCG@10. Run its test split once (--split test), apply item 11, and replace the tables and
   the recommendation above.
6. Delete the step 5-A project and record both dates in the release log.
7. For a hybrid or a switch:
   - on staging: run search-index create --tier M0;
   - on production: run search-index create --tier Flex --production, between 21:00 and 06:00 IST;
   - set NEARSHOP_SEARCH_SHADOW_ENGINES to the candidate and NEARSHOP_SEARCH_SHADOW_SHARE above 0;
   - shadow for at least 7 days and 300 searches, until every P5.6 guardrail passes in the AI Lab, including
     the time p95;
   - add those figures here.

   Serving the candidate to customers is a later release step. The settings validator then has to allow it in
   production. The products text index stays until a switch has served for 30 days (plan rule 4).

## Release-log items

- **On staging:** NEARSHOP_SEARCH_KEYWORD_ENGINE and NEARSHOP_SEARCH_VECTOR_ENGINE are unset (text and
  in_process); the API would refuse to start otherwise.
- **At the production deploy:** the same check, on production.
- **Each time create runs on staging or production:** the search-index status output, which must show at most
  3 indexes, within 3 on M0 and 10 on Flex.
- **With keep:** search-index status lists no search or vector index on staging or production.
