# Load test and capacity baseline

How fast the API is from India, how much load it takes, and where it should run, measured on staging.
Scorecard checks 4.4 (load test passes the section 8.11 targets) and 4.5 (region decision recorded with
measurements, Atlas in the same cloud region as the API); prompt P2.5; plan section 8.11. Due by G1
(13 Dec 2026).

**Status (20 Oct 2026): the tools are ready, but staging has not been measured yet.** Every result below
says "not measured" until the first staging run. A run needs staging working end to end (see the
release-and-rollback runbook) and staging test accounts. Check 4.4 and check 4.5 stay Fail until this page
has every number.

## What is measured

- **Load script.** backend/loadtest/locustfile.py, run with make loadtest. It goes through the staging
  Vercel URL, the way users reach the API. The script refuses to run against the production site, a Render
  URL, or any host not named in NEARSHOP_LOADTEST_CONFIRM_HOST.
- **Mix.**
  - SearchUser: anonymous search. Each query from backend/loadtest/queries.csv is typed with three
    suggest calls, then searched. About half the searches go on to the product page (detail and
    recommendations), and some of those to the shop page (detail, products, reviews). Think time is
    5 to 15 s.
  - OwnerUser: one per owner test account. It polls GET /api/owner/overview every 15 s, like the
    dashboard.
  - ReservationUser: one per customer test account. It reserves one in-stock pickup listing and cancels
    it at once, every 6 minutes. That is the plan's limit of 10 reservations per customer per hour.
- **Queries.** queries.csv holds 50 queries built from the catalogue's tags (backend/database/seed/data/
  catalog.json). Each of the 10 categories takes its most-shared tags in turn. Transliterated words such
  as beega, chhatri and joota are included. When the labelled query set from P5.1 exists, replace the file
  with it, keeping the query column.
- **Server-side time.** This comes from each response's Server-Timing header:
  - app: time in the API until the response starts;
  - db: wall time with database work in flight;
  - dbq: the total time the request's gathered queries waited in the 24-thread mongo-io pool's queue
    before a thread picked them up. Database.gather in backend/app/core/database.py measures this per
    task (P2.5). The same numbers are in each request's log line as duration_ms, db_ms and db_queue_ms.
- **End-to-end time.** Locust's response time on the load machine. Network is end-to-end minus app. That
  covers phone or laptop to the Vercel edge, the edge to Render, and Render's proxy. GET /favicon.ico is
  served by the edge alone, so its time is the network to the edge. The rest is edge to origin, the part a
  move to India would shorten.
- **Regions.** The x-vercel-id header gives the Vercel edge and function regions of every response.
- **Breaking point.** make loadtest-steps steps the search users through 30, 45, 60, 90, 120, 150 and 200,
  2 minutes each. The breaking point is the first step where end-to-end p95 exceeds 2 s or errors exceed 1%.
- **Pool queue wait p95.** The p95 of dbq, per endpoint, over the requests whose dbq is above 0. Many
  requests never queue (a single query, a transaction such as POST /api/reservations, a page with nothing
  to gather) and report 0; counting them would dilute the figure and could hide long waits on search. The
  target is judged twice: on search, and on the worst endpoint. dbq is a request's total over its gathered
  tasks, not a single task's wait, so this is not a per-task percentile.
- **Peak memory.** Locust cannot see it. Read it from Render: Dashboard > nearshop-api-staging > Metrics >
  Memory, the highest value during the run. For scale, the semantic index is about 2 MB at 1,351 listings
  and would be about 77 MB at 50,000. The embedding model also stays in memory.

The script prints a summary when the run stops and writes it as JSON to ~/.nearshop/loadtest/ (or to
NEARSHOP_LOADTEST_SUMMARY). The summary has:

- p50, p95 and p99 per endpoint, end to end and server side;
- network time;
- pool queue wait p95;
- error rates;
- the regions seen;
- each step;
- a pass or fail for every target Locust can measure.

Copy the numbers into the tables below.

## Before a run

1. **Staging sized like production.** Render > nearshop-api-staging > Settings > Instance Type: Standard
   (1 CPU, 2 GB), the production plan. Change it back to Starter after the run.
2. **Database tier.** Staging's Atlas cluster is M0 and production's is Flex. An M0 cluster has lower
   throughput and connection limits, so it can make the database look like the bottleneck when
   production's would not be. Either point staging at a temporary Flex cluster for the run, or record that
   the run used M0 next to any database bottleneck.
3. **Rate limits.** Keep NEARSHOP_RATE_LIMIT_MODE at log_only on staging (its default). All load comes from
   one or two IP addresses. At 30 users that is about 150 searches and 450 suggest calls (three per search)
   a minute from one address. Both are above the per-IP limits that real visitors behind carrier NAT also
   share: search_ip, 120 a minute, and suggest_ip, 240 a minute (backend/app/core/ratelimit.py). The
   product and shop pages come to about 280 calls a minute, close to catalog_read, 300 a minute per IP. Count the
   "would limit" log lines for each rule afterwards, and note them under Bottlenecks if real traffic could
   hit them.
4. **Test accounts.** Create staging test accounts as in the release-and-rollback runbook: 2 owners, each
   with a shop, and 2 customers, with random passwords. List them in a CSV file outside the repository,
   with the columns role,email,password (role owner or customer). Point NEARSHOP_LOADTEST_ACCOUNTS at it,
   with an absolute path: make loadtest counts the accounts from the repository root and Locust reads the
   file from backend/, so the script refuses a relative path, and stops if the file is missing. The script
   never prints them.
5. **Vercel protection.** If the staging preview is behind Vercel Authentication, set
   NEARSHOP_LOADTEST_VERCEL_BYPASS to the protection-bypass secret.
6. **Atlas region.** Check that the staging and production clusters are in Atlas's AWS Singapore region
   (ap-southeast-1), the same as Render's singapore region (render.yaml). Find it under Atlas > Database >
   the cluster > Overview > Region. Record it below.

## Running it

From the repository root, with backend dependencies installed (make setup), on two machines:

- **Load from a Bengaluru broadband connection.**
  NEARSHOP_LOADTEST_CONFIRM_HOST=<staging host> NEARSHOP_LOADTEST_ACCOUNTS=<file>
  make loadtest LOADTEST_URL=https://<staging host>
  This runs 30 search users plus one user per account for 10 minutes.
- **A probe from a laptop on a Ballari 4G connection, during the same 10 minutes.**
  NEARSHOP_LOADTEST_CONFIRM_HOST=<staging host> make loadtest LOADTEST_URL=https://<staging host>
  LOADTEST_USERS=2 LOADTEST_ARGS=SearchUser
  It adds two search users, so the 4G end-to-end times are measured under the same load.
- **Then, on its own, the breaking-point run from Bengaluru.**
  NEARSHOP_LOADTEST_CONFIRM_HOST=<staging host> make loadtest-steps LOADTEST_URL=https://<staging host>

Keep each summary file, and note the run's date, the staging commit (/api/health) and the instance type.

## Results

Run date: not run yet. Staging commit: not measured. API instance: not measured. Atlas tier: not measured.

### Per endpoint (milliseconds)

Server is app from Server-Timing. End to end is from Bengaluru broadband and from Ballari 4G.

| Endpoint | Server p50 / p95 / p99 | Bengaluru p50 / p95 / p99 | Ballari 4G p50 / p95 / p99 | Error rate |
|---|---|---|---|---|
| GET /api/search | not measured | not measured | not measured | not measured |
| GET /api/search/suggest | not measured | not measured | not measured | not measured |
| GET /api/products/{id} | not measured | not measured | not measured | not measured |
| GET /api/products/{id}/recommendations | not measured | not measured | not measured | not measured |
| GET /api/shops/{slug} | not measured | not measured | not measured | not measured |
| GET /api/shops/{slug}/products | not measured | not measured | not measured | not measured |
| GET /api/shops/{slug}/reviews | not measured | not measured | not measured | not measured |
| GET /api/owner/overview | not measured | not measured | not measured | not measured |
| POST /api/reservations | not measured | not measured | not measured | not measured |
| POST /api/reservations/{id}/cancel | not measured | not measured | not measured | not measured |

### Capacity

| Measure | Value |
|---|---|
| Peak memory (Render metrics) | not measured, out of 2 GB |
| Pool queue wait p95 (dbq, requests that queued), search / worst endpoint | not measured / not measured |
| Database time p95 for search (db) | not measured |
| Edge-only time p95 (favicon), Bengaluru / Ballari 4G | not measured / not measured |
| Vercel edge and function regions seen | not measured |
| Breaking point (p95 above 2 s or errors above 1%) | not measured |
| Requests the rate limits would have limited (log_only) | not measured |

### Targets (plan section 8.11)

| Target | Measured | Result |
|---|---|---|
| 30 concurrent search users for 10 minutes | not run | Fail: not measured yet |
| Server p95 for search of 600 ms or less | not measured | Fail: not measured yet |
| End-to-end p95 from India of 1,200 ms or less (the worse of the two vantage points) | not measured | Fail: not measured yet |
| Error rate below 0.5% | not measured | Fail: not measured yet |
| Peak memory of 75% of instance RAM or less (1,536 MB of 2 GB) | not measured | Fail: not measured yet |
| Pool queue wait p95 of 50 ms or less, on search and on the worst endpoint | not measured | Fail: not measured yet |

### Bottlenecks and follow-ups

Every failed target gets a row: its named bottleneck (pool queue, database round trips, CPU in ranking,
memory, network to Singapore, rate limits, or another) and the follow-up issue that fixes or accepts it.
For now, every target has failed only because the run has not happened.

| Target | Bottleneck | Follow-up issue |
|---|---|---|
| All six targets above | None identified yet: staging has not been measured | Run the staging load test (this page, P2.5), due before G1 |

## Region decision

The rule is to stay in Singapore if end-to-end p95 from India is 1,200 ms or less and network is 40% or
less of it. The script computes the network share for search as end-to-end p95 minus server p95, divided
by end-to-end p95. Apply the rule to the worse vantage point. Otherwise, write a one-page recommendation
here for moving the API and the database to a provider with an Indian region. In both cases, confirm that
the Atlas cluster is in the same region as the API.

| Item | Value |
|---|---|
| Search end-to-end p95, worse vantage point | not measured |
| Network share of it | not measured |
| Of which edge only (favicon) | not measured |
| API region | Render singapore (render.yaml) |
| Atlas region, production / staging | not confirmed / not confirmed |
| Decision | Not decided: waiting for the measurements |

## City scale (P8.2)

Plan section 8.11, "At city scale": the same targets at 50,000 listings and 500 shops. Scorecard check 4.4 at
that size. Prompt P8.2; the engine decision it follows is docs/search/engine-decision.md (P5.7: keep the text
index and the in-process vector index).

**Status (22 Oct 2026): the search changes and the data generator are in; the staging run at city scale has not
happened.** The five numbers below stay "not measured" until it has.

### What changed in search (backend/app/services/search.py)

- **Radius cap.** Search covers at most 25 km (MAX_RADIUS_KM). GET /api/search still accepts up to 50 km, so
  older links keep working, but searches 25, and the response's radius_km is the radius searched, so a client can
  tell. Proximity scoring uses the searched radius. The site's radius slider stops at 15 km.
- **Only the shops in radius.** The text-index query and the local-term match filter by the ids of the shops in
  radius. The in-process semantic index scores only the listings fetched in radius, with the same similarities
  as before. P5.7 kept both engines, so both stages needed this. The Atlas candidates already filter by shop.
- **Narrow reads.** Every listing in radius that passes the filters is read with seven fields: shop,
  catalogue item, category, price, quantity, stock confirmation time and photo key (RANK_FIELDS). Ranking,
  grouping, the counts and the map pins all work from these. Name, brand, unit, icon, MRP, low-stock
  threshold and the active flag (DISPLAY_FIELDS) are read only for the offers the page shows. Description,
  specs, keywords and local terms are never read.
- **Full-read bound.** A page holds at most 50 groups (MAX_PAGE_SIZE; the route rejects more, and run_search
  serves a larger internal page_size at 50), each showing its best offer and up to 6 offers, so one search reads
  at most 350 listings in full (MAX_FULL_READS). Paging never drops a group: every group appears on exactly one
  page.
- **Same ranking.** Groups, scores, totals, pins and category counts are exactly what the uncapped version
  gives. backend/tests/test_search_scale.py checks this on a test database with 2,198 active listings in radius,
  more than the cap: the top 20 match field for field in 20 cases, covering queries, filters, sorts, browsing and
  a second page.

**Deviation from the prompt, for sign-off.** P8.2 item 5 asks to "cap candidates at 2,000 by relevance before
grouping". That is not built: there is no cut of the candidate rows, so the rows grouped, scored and pinned still
grow with the listings in radius (as RANK_FIELDS rows; about 60 ms for 50,000 rows locally, below). The bound is on
full reads only, 350 per search. Why there is no cut before grouping: in the 50,000-listing database, 5 of the 50 load-test
queries pass the relevance cutoff with more than 2,000 listings. For example, "sports shoes" has 3,219 listings
in 19 groups, because many shops list the same catalogue item. Cutting at 2,000 listings before grouping would
drop whole groups from the top 20, or shrink their shop counts and price ranges. So every candidate goes into
grouping, using only its RANK_FIELDS, and the cap limits the full reads.

### The data generator

backend/loadtest/locustfile.py generate-city builds on the P5.7 --scale option. It takes a freshly seeded
database to 500 active shops and 50,000 active listings. Synthetic shops copy a seeded shop and stand within about
2 km of it, using the scale option's fixed random seed. Each one gets 100 or so listings: the copied shop's own,
then catalogue items it does not list yet. It runs only where a seed may run (the P1.1 guard): development and
test against a local MongoDB, or demo. It never runs in staging or production, and --confirm-db must repeat the
database name. Run it twice and nothing more is added.

    cd backend && NEARSHOP_ENVIRONMENT=<development|demo> NEARSHOP_MONGODB_URI=<uri> NEARSHOP_MONGODB_DB=<db> \
      uv run python loadtest/locustfile.py generate-city --confirm-db <db>

Locally, seed the database with make eval EVAL_DB=nearshop_eval_city, then run the generator against it. The
generator writes only shops and listings, so the snapshot's fingerprint still matches, and later make eval runs
with EVAL_DB=nearshop_eval_city measure at city scale.

### Local measurements (not the staging targets)

Measured on 22 Oct 2026 on a development laptop (Apple silicon), with a local MongoDB 8.2.2 single-node replica
set, all-MiniLM-L6-v2 embeddings, ranking r1 and the text plus in-process engines. Each figure is from a second
run, so seeding is not included. These are tooling figures: the dev split is still the 6 synthetic rows.

| Data | Code | Dev nDCG@10 | Zero-result | Search p95 | Ops per search | RSS after load / peak |
|---|---|---:|---:|---:|---:|---:|
| 1,351 listings, 49 shops (seed) | before | 0.8588 | 0.000 | 13.9 ms | 6.3 | not comparable |
| 1,351 listings, 49 shops (seed) | after | 0.8588 | 0.000 | 9.3 ms | 7.3 | 286 / 285 MB |
| 50,000 listings, 1,814 shops (--scale 50000) | before | 0.8703 | 0.000 | 408.9 ms | 9.3 | 549 / 621 MB |
| 50,000 listings, 1,814 shops (--scale 50000) | after | 0.8703 | 0.000 | 122.8 ms | 9.3 | 497 / 514 MB |
| 50,000 listings, 500 shops (generate-city) | after | 0.8650 | 0.000 | 101.8 ms | 9.3 | 487 / 506 MB |

Notes on the table:

- Before and after, every dev query returns the same ranked items and the same totals at both sizes.
- On the seed data, operations per search rise by one: the extra read of the shown offers. At 50,000 listings
  the count stays at 9.3, because the narrow read needs fewer getMore batches.
- "Before" memory at 1,351 listings is left out because that figure came from the seeding run.

**Test split, run once on the final version.** 1,351 listings: nDCG@10 0.8106, Recall@20 0.900, zero-result rate
0.000. P5.5 recorded r1 on the same rows at 0.8106 (docs/search/tuning-report.md). The drop is 0.000, inside
P8.2's 0.005 bound, and the zero-result rate did not rise.

**run_search time.** The 50 load-test queries plus one browse, each searched once from the city centre, on the
500-shop, 50,000-listing database:

| Radius | Code | p50 | p95 | Browse (no query) |
|---|---|---:|---:|---:|
| 5 km | before | 371 ms | 447 ms | 493 ms |
| 5 km | after | 128 ms | 159 ms | 225 ms |
| 25 km | before | 397 ms | 522 ms | 685 ms |
| 25 km | after | 116 ms | 230 ms | 151 ms |

Other test runs shared the laptop, so treat these figures as accurate to about ±50 ms. Before the change,
building a model for every listing in radius cost about 400 ms at this size. After it, the largest part is the
narrow read of every listing in radius, about 60 ms for 50,000 rows. A covering index for that read saved only
7 ms in a trial, so none was added (database/schema.py is unchanged).

**Memory.** The in-process semantic index still holds a vector for every active listing in the city, about
77 MB at 50,000, because it now scores only the rows in radius but keeps the whole matrix. Each API process holds
one. Atlas Vector Search, a P5.7 candidate, would remove it.

### Before the staging run at city scale

Staging cannot be filled by the generator, because the P1.1 guard refuses staging. Use a temporary database
instead:

1. Create a temporary Flex cluster for the run. It must not be listed in NEARSHOP_PROTECTED_HOSTS.
2. Seed it with the seed module under NEARSHOP_ENVIRONMENT=demo and --confirm-db (README).
3. Run generate-city against it, also under NEARSHOP_ENVIRONMENT=demo.
4. For the run only, point staging's NEARSHOP_MONGODB_URI and NEARSHOP_MONGODB_DB at the cluster, and resize
   staging to Standard (Before a run, step 1).
5. Use seeded accounts as the test accounts (Before a run, step 4).
6. Run make loadtest as above, first with the default 5 km and then with NEARSHOP_LOADTEST_RADIUS_KM=25, the
   worst case.
7. Afterwards, restore staging's URI and plan, and delete the cluster. Record both dates in the release log.

### Targets at 50,000 listings and 500 shops (plan section 8.11)

| Target | Measured | Result |
|---|---|---|
| Search server p95 of 600 ms or less | not measured | Fail: not measured yet |
| End-to-end search p95 from India of 1,200 ms or less | not measured | Fail: not measured yet |
| Error rate below 0.5% | not measured | Fail: not measured yet |
| Peak memory of 75% of instance RAM or less (1,536 MB of 2 GB) | not measured | Fail: not measured yet |
| Pool queue wait p95 of 50 ms or less | not measured | Fail: not measured yet |

## Tooling check (not a measurement)

This only checks that the scripts work. It is not a capacity figure. On 20 Oct 2026 the script ran for
40 s on a development laptop against a local API and a local MongoDB with the seeded Ballari dataset:
6 search users, 1 owner and 1 customer, with think times shortened to 1 to 3 s.

- Every scenario ran with no errors. Signing in, polling the overview, and reserving then cancelling all
  worked.
- The favicon probe fails locally, because only Vercel serves that file.
- Server-Timing was read on every API response. Search server p95 was 46 ms, and dbq p95 over all
  requests (before the per-endpoint rule above) was 2.4 ms.
- The stepped shape ran 5 then 15 users and reported each step.
- The script refused a host that was not confirmed, and sent no requests to it.

These numbers say nothing about staging, Atlas, or the network from India.
