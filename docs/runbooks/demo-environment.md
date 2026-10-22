# Demo environment and production without synthetic data (P7.1)

Production holds only real data. The synthetic Ballari city (49 fictional shops, 1,351 listings, 109,096 sales
flagged is_demo, and the demo accounts on nearshop.demo) lives in a separate demo deployment, which the research
paper (paper/) and the AI Lab need. Scorecard check 10.1.

## What runs where

| | Production | Demo |
|---|---|---|
| Frontend | Vercel project of the live site, branch release | Its own Vercel project, branch release |
| API | Render nearshop-api (Standard), NEARSHOP_ENVIRONMENT=production | Render nearshop-api-demo (Free), NEARSHOP_ENVIRONMENT=demo |
| Database | Production cluster (Flex), database nearshop_prod | Its own M0 cluster, database nearshop_demo |
| Data | Real shops, users and sales; the catalogue from the catalogue bootstrap | The seed's synthetic city, reseeded every night |
| Robots | NEARSHOP_INDEXING true from launch day (plan B14) | NEARSHOP_INDEXING unset: robots.txt disallows everything |
| Demo banner and footer sentence | Off (NEARSHOP_DEMO_MODE=false) | On (NEARSHOP_DEMO_MODE=true) |
| Login demo-account panel | Off (NEXT_PUBLIC_NEARSHOP_DEMO_MODE unset) | On (NEXT_PUBLIC_NEARSHOP_DEMO_MODE=true) |
| AI Lab (/ai, /api/ai/*) | Admins only; everyone else gets 404. Admins see only model cards trained on real data | Everyone |
| Search comparison (/api/ai/search-compare) | Admins only (404 for everyone else) | Everyone |
| Retrain (POST /api/ai/retrain) | Admins only | Admins only (and no retrain cron: the nightly reseed trains) |

Who may see the AI Lab is the API's setting NEARSHOP_AI_LAB_PUBLIC: unset, it is true in development, test and
demo and false in staging and production (backend/app/core/config.py). The /ai page asks GET /api/ai/access
before it renders and answers 404 when the API does. The footer links to the AI Lab only when the lab is public
or the visitor is an admin, and shows the generated-data sentence only while the API reports demo_mode.

## URLs

Fill these in when the services exist (the team records them here and in the release log):

- Demo site: https://nearshop-demo.vercel.app (the demo Vercel project's production domain)
- Demo API: https://nearshop-api-demo.onrender.com (Render, nearshop-api-demo)
- Live site: https://nearshop-umber.vercel.app until the own domain is live

The demo API sleeps after 15 idle minutes (Render free plan) and the first request after that takes about a
minute. That is acceptable for a demo and never for production.

## One-time set-up of the demo

1. **Atlas.** Create a separate project with an M0 cluster (region Mumbai or Singapore), a database user for the
   demo only, and network access for Render. The database is nearshop_demo; MongoDB creates it on the first write.
   Never reuse the staging or production project, user or password.
2. **Render.** The Blueprint (render.yaml) has nearshop-api-demo and nearshop-reseed-demo. On the first sync
   Render asks for the sync: false values:
   - nearshop-api-demo: NEARSHOP_SECRET_KEY (its own), NEARSHOP_MONGODB_URI (the demo cluster),
     NEARSHOP_PROTECTED_HOSTS (the staging and production Atlas project domains, so the reseed can never reach
     them), NEARSHOP_TOTP_KEY (its own);
   - nearshop-reseed-demo: NEARSHOP_DEMO_PASSWORD (a random value of at least 16 characters, kept in the team
     password manager; the demo accounts get it), and NEARSHOP_DEMO_API_DEPLOY_HOOK_URL (nearshop-api-demo's
     deploy hook, from its Settings; the reseed restarts the demo API with it, see "Reseed schedule").
3. **First seed.** Trigger a run of nearshop-reseed-demo (Dashboard > nearshop-reseed-demo > Trigger Run). It
   resets nearshop_demo (which also runs the schema migrations), writes the city and trains the models, in a few
   minutes. Then open the demo API's /api/health/ready.
4. **Vercel.** New project from the same repository, branch release, root frontend. Environment:
   NEARSHOP_API_ORIGIN = the demo API's URL, NEARSHOP_EDGE_SECRET unset (the demo API does not require it),
   NEXT_PUBLIC_NEARSHOP_DEMO_MODE = true, NEARSHOP_INDEXING unset, NEARSHOP_SITE_URL = the demo URL.
5. **Check.** The demo's /robots.txt answers "Disallow: /", the login page shows the demo-account panel, a
   customer demo account signs in, and /ai shows the model cards to a signed-out visitor.

The seeded admin has no second factor and cannot sign in (P2.4). The demo's public accounts are the customer
(priya@nearshop.demo) and the featured shop's owner (owner@nearshop.demo), with the demo password.

## Reseed schedule

nearshop-reseed-demo runs every night at 22:30 UTC (04:00 IST):

    uv run --no-dev python -m database.seed.seed --confirm-db "$NEARSHOP_MONGODB_DB"

with NEARSHOP_ENVIRONMENT=demo. The P1.1 guard lets it run only in demo, development and test, never against a
host in NEARSHOP_PROTECTED_HOSTS and never against the production database name (nearshop_prod). Everything a
visitor changed during the day (reservations, reviews, stock edits) is gone after the reseed, and the demo
accounts work again with the demo password. A failed run leaves the demo as the reset left it: trigger the job
again.

After a successful reseed the job calls nearshop-api-demo's deploy hook (NEARSHOP_DEMO_API_DEPLOY_HOOK_URL on
the cron job: copy it from nearshop-api-demo > Settings > Deploy Hook), so the API starts again and rebuilds
its in-process search index from the new data; a failed hook call fails the job. With the variable blank there
is no restart: if search on the demo looks stale after a reseed, restart nearshop-api-demo by hand (Dashboard >
Manual Deploy > Restart service).

Schema: the free instance has no pre-deploy step, so its start command runs python -m database.migrate before
the server starts. A release that adds migrations is applied to nearshop_demo when the new demo instance
starts, and again (as a no-op) on each wake from sleep.

## Reproducing the paper's figures

The seed is deterministic (random generators seeded with 42; dates relative to the day it runs), so every
reseed gives the same shops, listings and sales pattern. The figures in paper/figures come from the demo:

- **nearshop_shop_discovery.png**: the demo site's /search near the Ballari centre, as a signed-out visitor.
- **nearshop_ai_lab.png** and **nearshop_model_cards.png**: the demo site's /ai after the nightly reseed (the
  reseed trains every model, so the cards show that night's run; numbers move slightly from day to day because
  the sales window ends on the reseed date).
- **Search quality tables**: make eval (backend/eval/README.md) on a laptop's local database, which seeds its
  own copy; the committed backend/eval/published.json is what the AI Lab shows.

To reproduce offline: make seed (local database, with NEARSHOP_DEMO_PASSWORD in backend/.env), make api and
make web, then take the same screenshots from http://localhost:3000.

## Switching production to real data (after release, by 21 Feb 2027)

Production holds only the seed's data until this switch, so no real record is lost. Each step is recorded in
the release log (P1.3).

1. **Keep the old data.** Trigger nearshop-backup-production and check its row in backup_runs. That set is the
   archive of the synthetic production database. Prove it restores with the restore drill
   (docs/runbooks/restore.md, Restore drill) into nearshop_drill_<yyyymmdd> on the production cluster. The old
   database (nearshop) is not deleted until that drill has passed. The demo itself does not need this data: it
   regenerates the same city every night (above). The restore tool refuses a production set outside production,
   so production data never moves to the demo cluster.
2. **Create the fresh database.** On nearshop-api, run two one-off jobs (Render docs, "One-off jobs"; run_job as
   in docs/runbooks/restore.md, with the nearshop-api service id), each with NEARSHOP_MONGODB_DB=nearshop_prod
   in the command:

       run_job 'NEARSHOP_MONGODB_DB=nearshop_prod uv run --no-dev python -m database.migrate'
       run_job 'NEARSHOP_MONGODB_DB=nearshop_prod uv run --no-dev python -m database.seed.seed --catalogue-bootstrap --confirm-db nearshop_prod'

   The catalogue bootstrap upserts the categories and catalogue items (by slug, with their Kannada, Telugu and
   Hindi terms) and never deletes. It writes no shops, users, sales or searches, and it refuses to run unless
   --confirm-db repeats the database name and the migrations have run. Its one write outside the catalogue:
   listings linked to a catalogue item get that item's local terms copied onto their local_terms field (as
   migration m0008 does on every migrate); on the fresh database there are none. It is safe to run again later
   to add catalogue items; on a database with real listings, its output's local_terms_listings says how many
   listings' local terms it changed.
3. **Switch.** One pull request changes render.yaml for production: NEARSHOP_MONGODB_DB to nearshop_prod on
   nearshop-api and on its backup, archive and retrain cron jobs, and NEARSHOP_DEMO_MODE to "false" on
   nearshop-api. Merge to main, check staging, fast-forward release (docs/runbooks/release-and-rollback.md).
   The pre-deploy migration runs against nearshop_prod. On the live Vercel project, check that
   NEXT_PUBLIC_NEARSHOP_DEMO_MODE is unset.
4. **Admins.** Create the admins again in the fresh database with admin_tasks create-admin (one-off job on
   nearshop-api), at most 3, each enrolling the second factor.
5. **Check.** As a one-off job on nearshop-api:

       run_job 'uv run --no-dev python -m database.admin_tasks demo-check'

   It counts the documents flagged is_demo in every collection and the users on nearshop.demo. Exit 0 and
   "RESULT: no synthetic data." is the pass for check 10.1; exit 1 means the database holds synthetic data.
   Then, signed out, open the live site's /ai: it must answer 404.
6. **Afterwards.** Drop the old nearshop database only after step 1's drill passed and the switch has run for a
   week without a rollback.

## Troubleshooting

- **The reseed job says "NEARSHOP_ENVIRONMENT is 'production'"** or names a protected host: its settings point
  at the wrong cluster. Fix nearshop-api-demo's NEARSHOP_MONGODB_URI; nothing was written.
- **"NEARSHOP_DEMO_PASSWORD is not set"**: set it on nearshop-reseed-demo (Environment).
- **The catalogue bootstrap says "has no schema yet"**: run the migrate job of step 2 first.
- **/ai answers 404 on the demo**: NEARSHOP_AI_LAB_PUBLIC is set to false on nearshop-api-demo, or the demo
  Vercel project's NEARSHOP_API_ORIGIN is wrong (the page answers 404 when it cannot ask the API).
