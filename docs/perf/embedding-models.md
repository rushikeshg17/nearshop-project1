# Multilingual embedding model trial

Whether search should move from all-MiniLM-L6-v2 (English only) to a multilingual embedding model.
Scorecard checks 8.4 and 8.2; prompt P5.3; plan section 8.6 (the evaluation rules).

**Decision (21 Oct 2026): keep sentence-transformers/all-MiniLM-L6-v2.** Neither candidate met the dev-split
adoption rule below, so neither went to the test split, and the test split was not run. The dev split today
holds only the 6 synthetic example rows from P5.1: the B15 labelled set (due 10 Jan 2027) and its stored
baseline do not exist yet, so these quality figures cannot justify a switch either way. **Repeat the quality
rows of this trial on the labelled dev split once B15 delivers it** (the memory, latency and build-time rows
need only re-measuring on Linux, see "Not measured yet").

## How a model is chosen

- NEARSHOP_EMBEDDING_MODEL names the model. It must be listed in EMBEDDING_MODELS in
  backend/app/core/config.py, which records for each model its query and passage prefixes and its
  similarity calibration (floor and ceiling). The API refuses to start with an unlisted name, so a model
  that needs prefixes is never run without them.
- Prefixes: multilingual E5 models need "query: " on search queries and "passage: " on listing texts; the
  other listed models need none (fastembed 0.8.1's model list says so). app/ai/embeddings.py adds them;
  the semantic index embeds listings as passages and searches as queries.
- Vectors are cached in text_embeddings under "<model>:<sha256 of text>", so several models' vectors coexist.
  Switching models embeds every listing once under the new name; the old vectors stay until deleted.
- Calibration: app/services/search.py ranks on the all-MiniLM-L6-v2 scale (SEM_FLOOR 0.22, SEM_CEIL 0.62).
  The semantic index maps another model's similarities onto that scale linearly with the model's own floor
  and ceiling from config. Every candidate is on the current calibration (0.22 and 0.62), untuned, as P5.3
  requires; tuning is P5.5. A test keeps config's reference scale equal to search.py's constants.
- TF-IDF (NEARSHOP_EMBEDDING_PROVIDER=tfidf, or the fallback when the model cannot load) is unchanged.

## Candidates

fastembed 0.8.1 (the installed version) lists four multilingual text models:

| Model | Dimensions | Download | Prefixes | Trialled |
|---|---:|---:|---|---|
| sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (quantised ONNX) | 384 | 0.22 GB | none | yes |
| minishlab/potion-multilingual-128M (static embeddings) | 256 | 0.51 GB | none | yes |
| sentence-transformers/paraphrase-multilingual-mpnet-base-v2 | 768 | 1.0 GB | none | no: the weights alone are half the 2 GB instance, and L12, a quarter of its size, already takes the API to 788 MB |
| intfloat/multilingual-e5-large | 1024 | 2.24 GB | query: / passage: | no: larger than the instance's 2 GB of RAM |

The prompt also names multilingual E5 small and base variants; fastembed 0.8.1 does not list them, so they
were not trialled (the prompt allows only listed models). The baseline is
sentence-transformers/all-MiniLM-L6-v2 (384 dimensions, 0.09 GB).

## Method

- **Quality.** make eval's harness (backend/app/ai/search_eval.py) on the dev split, once per model, with
  NEARSHOP_EMBEDDING_MODEL set and everything else equal: the same seeded evaluation database (the
  deterministic Ballari seed, 1,351 active listings), branch lane-search after P5.2. Runs were not stored
  (--no-store) because they are trial runs. The dev split is 6 synthetic example rows, one per language tag.
- **Query embedding p95.** One query embedded (with the query prefix, if any) and scored against the
  1,351-listing matrix, 300 times after 5 warm-up queries, on one ONNX Runtime thread (OMP_NUM_THREADS=1,
  threads=1), as on the production instance's 1 CPU. The whole search request adds the keyword stage and
  the database round trips on top (make eval's p95 column). Queries: the example rows plus 15 short queries in
  English, Kannada, Telugu, Devanagari and Latin transliteration.
- **Resident memory after load.** The API process (app.main imported), the model loaded, the index built
  and 60 searches through run_search (not logged), read with psutil. This is the "with the rest of the API"
  figure. The budget is 75% of 2 GB, 1,536 MB.
- **Index build time.** A cold build: every listing text embedded as a passage on one thread, no cache. At
  1,351 listings (the seed) and at 50,000 synthetic listings (the seed's texts, each made unique with a
  pack-size word and a number). A warm rebuild reads text_embeddings and embeds only new texts.
- Machine: Apple M4 (arm64), macOS, one thread for timings. Not the production instance; see "Not measured
  yet".

## Results

### Dev nDCG@10 (6 synthetic rows; one query per language)

| Model | All | en | kn | kn-Latn | te-Latn | hi-Latn | misspelt | Zero-result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| all-MiniLM-L6-v2 (baseline) | 0.859 | 0.976 | 0.982 | 1.000 | 0.922 | 0.944 | 0.329 | 0.000 |
| paraphrase-multilingual-MiniLM-L12-v2 | 0.860 | 0.963 | 0.982 | 1.000 | 0.845 | 0.963 | 0.406 | 0.000 |
| potion-multilingual-128M | 0.860 | 0.963 | 0.982 | 1.000 | 0.845 | 0.859 | 0.513 | 0.000 |

The Kannada-script example ("bike oil" in Kannada) is found by P5.2's local-term keyword match whatever the
model, so its figure cannot move here.

### Cost

| Model | Query p50 | Query p95 | API resident memory | Share of 2 GB | Cold build, 1,351 | Cold build, 50,000 |
|---|---:|---:|---:|---:|---:|---:|
| all-MiniLM-L6-v2 (baseline) | 1.3 ms | 1.6 ms | 302 MB | 15% | 5.2 s | 204 s |
| paraphrase-multilingual-MiniLM-L12-v2 | 3.4 ms | 4.3 ms | 788 MB | 38% | 34.4 s | about 1,270 s (estimated, see below) |
| potion-multilingual-128M | 0.11 ms | 0.14 ms | 1,073 MB | 52% | 0.11 s | 6.2 s |

Peak resident memory during those runs: 302 MB, 846 MB and 1,091 MB.

Notes on these figures:
- The baseline and potion rows come from one uninterrupted run each (wall time on one thread). The L12 run
  was throttled by macOS part-way (its process got a few per cent of a core), so L12's query and 1,351
  timings were re-measured in CPU time on one thread, which the throttling does not inflate. A CPU-time
  re-run of the baseline in the same session gave 1.5 ms p50, 1.7 ms p95 and 9.6 s for the 1,351 build,
  so the session's own variation is up to about 2x on build time; the ordering of the models is not in
  doubt.
- **L12 at 50,000 is an estimate, not a measurement.** Two attempts to time it were throttled the same way
  and stopped. The figure is the measured 1,351-listing cost (25.5 ms per text, one thread) times 50,000.
  Embedding cost is linear in the number of texts at a fixed batch size (the baseline's two figures,
  3.9 ms and 4.1 ms per text, show it). Measure it on staging before L12 is ever adopted.
- The "cold build" embeds every listing text. The seed's 1,351 listings have only 317 distinct texts, and
  the index embeds each distinct text once, so a real cold build of the seed is about a quarter of the
  1,351 figure.
- The quantised L12 ONNX model is about 6 times slower per text than the baseline on one Arm thread;
  potion is a static (lookup and average) model, so it is fast but holds a 0.5 GB table in memory.

## The adoption rule, applied

A candidate goes to the test split only if all four hold on the dev split against the baseline (P5.3):

| Condition | L12 | potion |
|---|---|---|
| Overall nDCG@10 rises by 0.03 or more | Fail (+0.001) | Fail (+0.002) |
| Kannada-subset nDCG@10 rises by 0.10 or more | Fail (+0.000) | Fail (+0.000) |
| Memory with the rest of the API is 75% of RAM or less | Pass (38%) | Pass (52%) |
| Query p95 is 50 ms or less | Pass | Pass |
| **Goes to the test split** | **No** | **No** |

Neither candidate went to the test split, so the second step (test nDCG@10 up by 0.02 or more and the
zero-result rate not rising, plan section 8.6) did not arise, and the test split was not run.
**Decision: keep all-MiniLM-L6-v2**; render.yaml keeps downloading it at build time (unchanged command; it
downloads whatever NEARSHOP_EMBEDDING_MODEL names).

Why this is not yet a verdict on the models: 6 synthetic rows, one per language, cannot show a 0.10 Kannada
gain or rule one out. Both candidates fit the memory and latency budgets, so both stay listed in config and
should be re-run on the B15 dev split.

## Not measured yet

- **Quality on the labelled set (B15, due 10 Jan 2027).** Re-run, per model:
  NEARSHOP_EMBEDDING_MODEL=<model> make eval EVAL_ARGS=--no-store, and fill a new dev table; then run the
  test split once for a candidate that passes, and only for it.
- **The production instance size.** The figures above are from an Apple M4 on one thread, under macOS. Query
  p95 on Render's 1-CPU Standard instance will be higher (a shared x86 core is slower than an M4 core), and
  Linux resident memory differs from macOS. Before adopting a model, measure on staging resized to the
  production plan: the Render memory graph after warm-up and 60 searches, and Server-Timing for searches.
  The latency margin is wide (1.6 ms against a 50 ms limit), the memory margin less so for potion (52%).
- **Build time on the instance.** A model switch re-embeds every listing once, in the API process, at the
  first index build after deploy. At 50,000 listings, MiniLM L12 would take about 21 minutes on one M4
  thread (estimated above); on the instance it will be longer, so a switch at that size should pre-fill text_embeddings first.
