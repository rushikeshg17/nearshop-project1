# Backups and restore

Production data can be restored from a copy no older than 24 hours, within 2 hours. Scorecard checks 3.3
(Atlas snapshots plus a nightly off-site export, RPO 24 hours or less) and 3.4 (a restore drill in the last
90 days with an RTO of 2 hours or less and counts matching the manifest); plan sections 8.4, 8.10 and 8.12.

## Targets

| | Target | How it is met |
|---|---|---|
| RPO (data that may be lost) | 24 hours or less | A nightly off-site set at 21:30 UTC (03:00 IST), plus the Atlas Flex daily snapshot on production |
| RTO (decision to production serving restored data) | 2 hours or less | The restore below, proven by a drill every quarter (log at the end) |

## What is kept

| Copy | Where | How often | Kept | Point in time |
|---|---|---|---|---|
| Atlas snapshot (production, Flex) | Atlas, with the cluster | Daily, taken by Atlas | The last 8 (Atlas docs); no point-in-time restore | One instant, whole cluster |
| Off-site set (production and staging) | The private Cloudflare R2 backup bucket, under production/ or staging/ | Nightly, the nearshop-backup-production and nearshop-backup-staging cron jobs (render.yaml) | The newest set of each of the last 30 days and the first set of each of the last 12 months | Collection by collection over the few minutes of the run |
| Archived sales history (production and staging) | The same backup bucket, under production/archive/sales_history/ or staging/archive/sales_history/ | Monthly, the nearshop-archive-production and nearshop-archive-staging cron jobs (render.yaml) | Indefinitely: nothing deletes an archived month | One calendar month per part, as it was on the run's day |

Staging runs on M0, which has no Atlas backups (MongoDB's pricing FAQ), so the off-site set is its only copy.

An off-site set (backend/database/backup.py) is one prefix, `<environment>/<set id>/`, where the set id is
the UTC start time (2026-10-21T213000Z). It holds one gzipped BSON file per collection
(`<collection>.bson.gz`, the mongodump format, so `mongorestore --gzip --db <db> --collection <name> <file>`
can also read it) and `manifest.json`, written last: document counts, validators and options, index specs,
the latest migration number, the git commit, and each file's size and SHA-256. A set without a manifest is
incomplete and is never restored; it is deleted once it is a day old. Every collection is copied except the
migration lock and backup_runs. counters is read last, so a restored database never issues an id twice.

The sets hold personal data and password hashes. The buckets are private, each token is scoped to its own
bucket, and no laptop holds a backup token or a staging or production connection string. Data deleted from
the database ages out of the sets by the retention dates above (plan section 8.4; the privacy notice says
so).

**Heartbeat.** Every run writes a row to `backup_runs` in the database it backs up: `_id` (the set id),
`started_at`, `finished_at`, `outcome` (running, succeeded or failed), `size_bytes`, `documents`,
`collections`, `deleted_sets` and, on failure, `error`. Alert when the newest `succeeded` row is more than
26 hours old (plan section 8.10). The alert is a Better Stack heartbeat that the cron job pings only after a
run succeeds (NEARSHOP_BACKUP_HEARTBEAT_URL; docs/runbooks/monitoring.md, alert 12). To look by hand: Atlas > the cluster > Browse
Collections > nearshop > backup_runs, sorted by `started_at` descending.

## One-time set-up (plan step 1-A, R2)

1. **Buckets.** In Cloudflare R2 create a private bucket for production backups and one for staging
   backups (no public access, no custom domain), and an API token for each, Object Read & Write on that
   bucket only. Separate buckets keep staging's token from reading production's customers; if the team
   keeps a single backup bucket, the prefixes still separate the sets, but staging's token can then read
   production's sets, so give staging its own bucket before production holds real customer data.
2. **Render.** Sync the Blueprint (render.yaml). It creates nearshop-backup-staging (branch main) and
   nearshop-backup-production (branch release). Each reads NEARSHOP_MONGODB_URI and
   NEARSHOP_MEDIA_S3_BUCKET from its API service; the tool refuses a backup bucket equal to that photo
   bucket. For
   each, enter NEARSHOP_BACKUP_BACKEND=s3 and its own NEARSHOP_BACKUP_S3_ENDPOINT
   (`https://<account id>.r2.cloudflarestorage.com`), NEARSHOP_BACKUP_S3_BUCKET,
   NEARSHOP_BACKUP_S3_ACCESS_KEY_ID and NEARSHOP_BACKUP_S3_SECRET_ACCESS_KEY. On a service that already
   exists, add them in the dashboard (Environment): a Blueprint sync does not prompt for new sync: false
   values.
3. **First run.** For each cron job, Dashboard > the job > Trigger Run. The log ends with a JSON line
   (`"outcome": "succeeded"`, the documents, size and seconds), and backup_runs has a succeeded row.
4. **Render API key** for the one-off jobs below: Account Settings > API Keys. Keep it in the team password
   manager, never in the repository.

## Which copy to restore

Use the **Atlas snapshot** when:
- the production cluster is damaged or data was wiped or corrupted, Atlas itself is working, and the
  newest snapshot is newer than the newest off-site set;
- the whole cluster must return to one consistent instant.

Use the **off-site set** when:
- Atlas, the project or the cluster is gone or locked (deleting a cluster can delete its snapshots with
  it), or the account cannot be reached;
- the damage is older than the 8 snapshots (noticed after more than 8 days): pick a daily set from the last
  30 days or a monthly one;
- only some records are needed back: restore a set into a scratch database and copy them across by hand;
- staging needs restoring (M0 has no snapshots);
- moving to another cluster or provider (plan step 1-A moves the current database to the new production
  cluster this way);
- the quarterly drill.

Whatever the copy, writes made after it was taken are lost. Before restoring, note the time, and tell the
shops affected afterwards which reservations and stock changes to re-enter.

## Restore from an off-site set

The tool is backend/database/restore.py. It restores into a database on the cluster in NEARSHOP_MONGODB_URI,
named by `--target-db`, and it refuses, before writing anything:
- a target that has any collection, even an empty one: name a database that does not exist yet (MongoDB
  creates it on the first write; do not create it in Atlas first);
- the production database name (NEARSHOP_PRODUCTION_MONGODB_DB, and in production NEARSHOP_MONGODB_DB, the
  database the API serves), unless `--emergency` is given and the operator types the name when asked;
- a production set anywhere but in production (NEARSHOP_ENVIRONMENT=production);
- a file whose size or SHA-256 differs from the manifest, and a set whose migration is newer than the code.

It then rebuilds the database as it was at the set's migration, never with the code's newer schema: it
creates each collection with the options in the manifest (validators included), inserts the documents in
batches (counters and schema_migrations included), checks every collection's count against the manifest
(any difference: exit 1, "Restore FAILED"), builds the set's indexes from the manifest, raises any counter
behind its collection, and only then runs the migration runner: migrations newer than the set, then the
code's validators and indexes, as a deploy would. A new unique or TTL index in newer code is therefore
built after the old documents are in and migrated. It prints a JSON summary with `total_seconds`. Exit 2
means refused, nothing written.

Restores run as **Render one-off jobs** on the backup cron job of the environment (Render docs, "One-off
jobs"): a job uses the service's build and settings, so the database URI and the backup token never leave
Render. In the commands below, `$RENDER_API_KEY` is the key from set-up step 4 and `$SERVICE_ID` is the cron
job's id (Dashboard > nearshop-backup-production > the `crn-...` id in the URL). Each job's output is in the
cron job's Logs in the dashboard.

```
run_job() {  # usage: run_job '<command>'
  curl -sS -X POST "https://api.render.com/v1/services/$SERVICE_ID/jobs" \
    -H "Authorization: Bearer $RENDER_API_KEY" -H "Content-Type: application/json" \
    -d "{\"startCommand\": \"$1\"}"
}
```

**Production, step by step.** Start the clock at the decision and write the times down.

1. **Stop new damage.** If the damage is still happening (a bad release, a runaway job), roll back the
   release first (docs/runbooks/release-and-rollback.md, Rollback). Record the time.
2. **Choose the set.** `run_job 'uv run --no-dev python -m database.restore --list'`. Pick the newest
   complete set taken before the damage.
3. **Restore into a new database** on the production cluster, named for the day, never the live name:

   ```
   run_job 'uv run --no-dev python -m database.restore --target-db nearshop_restored_20261121 --set 2026-11-20T213000Z'
   ```

   Leave out `--set` for the newest complete set. The production Flex cluster has room for a second copy
   (5 GB; about 200 MB is used). To restore onto another cluster instead (Atlas itself is gone), create the
   cluster, set the cron job's NEARSHOP_MONGODB_URI to it for the job's duration (Environment, replacing the
   fromService value), and set it back afterwards.
4. **Check** the job's log: exit 0 and the JSON summary, whose documents match the manifest. On "Restore
   FAILED", drop that database (Atlas > Browse Collections > the database > Drop) and run step 3 again,
   with an older set if a file is damaged.
5. **Switch the API to it.** Dashboard > nearshop-api > Environment: NEARSHOP_MONGODB_DB =
   nearshop_restored_20261121, Save and deploy (the pre-deploy migration step runs against it). Set the same
   value on nearshop-backup-production, so tonight's backup copies the restored database. In the same day,
   change both `NEARSHOP_MONGODB_DB` values for production in render.yaml through a normal pull request, or
   the next Blueprint sync sets them back to nearshop.
6. **Verify.** https://nearshop-umber.vercel.app/api/health answers, the read-only production smoke passes
   (`make smoke`), and an owner and a customer account look right. Stop the clock: this is the RTO.
7. **Afterwards.** Keep the damaged database until the incident write-up is done, then drop it. Trigger a
   backup run (Dashboard > nearshop-backup-production > Trigger Run) and check backup_runs. Add the incident
   to the drill log below with its time.

**Emergency restore into the live name.** Only when the database name itself cannot change. The target must
still be empty, so first drop the damaged database (or rename what must be kept). The tool then asks the
operator to type the name, so it needs a terminal: a one-off job cannot answer and is refused. Use
Dashboard > nearshop-api > Shell, enter the backup settings for that one command without echoing the secret,
and never paste them into a file:

```
cd /opt/render/project/src/backend   # if the shell starts elsewhere
read -rs NEARSHOP_BACKUP_S3_SECRET_ACCESS_KEY && export NEARSHOP_BACKUP_S3_SECRET_ACCESS_KEY
NEARSHOP_BACKUP_BACKEND=s3 NEARSHOP_BACKUP_PREFIX=production \
NEARSHOP_BACKUP_S3_ENDPOINT=https://<account id>.r2.cloudflarestorage.com \
NEARSHOP_BACKUP_S3_BUCKET=<production backup bucket> NEARSHOP_BACKUP_S3_ACCESS_KEY_ID=<key id> \
  uv run --no-dev python -m database.restore --target-db nearshop --set <set id> --emergency
```

Close the shell afterwards.

**Staging** is the same with nearshop-backup-staging, the staging/ prefix and nearshop-api-staging. A
production set never restores into staging: staging holds generated data only.

**A laptop** restores only its local database, from a local directory: `make backup`, then
`make restore TARGET_DB=nearshop_restored` (Makefile). With NEARSHOP_ENVIRONMENT development or test, both
tools refuse any MongoDB that is not local.

## Restore from an Atlas snapshot

Atlas > the production project > the cluster > Backup > Snapshots: choose the snapshot > Restore, and pick
the target cluster. A snapshot restore replaces the data on the target cluster, so restore into a new cluster when
the current data might still be needed, then point the API at it: nearshop-api > Environment >
NEARSHOP_MONGODB_URI, and the same on nearshop-backup-production (it reads the API's value, so a change there
carries over). Then follow steps 6 and 7 above. The Atlas docs on Flex cluster backups describe the current
screens.

## Archived sales history (P8.3)

sales_history grows without limit (imports from 50 shops could add about 0.5 GB a year), while the Atlas Flex
limit is 5 GB and the models use at most the last 18 months. So once a month backend/database/archive.py
moves the older months out of Atlas, into the backup bucket, where they can be brought back.

**What runs.** The nearshop-archive-production and nearshop-archive-staging cron jobs (render.yaml), at
23:00 UTC on the 1st of each month, 90 minutes after that night's backup. A month (UTC, as sold_at is
stored) is eligible once it ends on or before the first of the current month less 18 months
(NEARSHOP_ARCHIVE_KEEP_MONTHS, never below 18): a run on 1 Nov 2026 archives April 2025 and every earlier
month still in the database. Months go one at a time, oldest first:
1. the month's rows, sorted by _id, are written byte for byte to a gzipped BSON file (the backup format);
2. the file is uploaded to `<prefix>/archive/sales_history/<YYYY-MM>/<part id>.bson.gz`, downloaded again,
   and must match its size and SHA-256 and hold exactly the rows read, by _id;
3. `<part id>.manifest.json` is uploaded and read back: month, date range, documents, size, SHA-256, a
   SHA-256 of the _ids, first and last _id, database, environment, git commit and migration number;
4. only then are those rows (by _id) deleted from sales_history.

The part id is the run's UTC start time (2026-11-01T230000Z). A failure in steps 1-3 stops the run and
leaves that month's rows in the database (exit 1, "Archive failed"). A failure during step 4 (the deletes go
in batches) leaves the month partly in Atlas but fully archived: its data file and manifest are already
complete and verified in the bucket, and the next run archives the rows still in Atlas as a new part of that
month (a restore puts each row back once). Either way, months finished before it stay archived. A run
is refused, with nothing written or deleted (exit 2), while NEARSHOP_BACKUP_BACKEND is off, and when the
newest succeeded row in backup_runs is more than 26 hours old (NEARSHOP_ARCHIVE_BACKUP_MAX_AGE_HOURS), so
every archived row is also in a recent backup set (and in the monthly sets for the next 12 months).

Archived files sit outside every backup set (`<prefix>/<set id>/`), so backup retention never deletes them,
and no tool deletes them (out of scope for P8.3). A part without its manifest is incomplete (a run that
failed before step 3); it is never restored, and its rows are still in the database and go in a new part.
Rows that reach an archived month later, such as an old sales report imported after the run, go in a new
part of that month in the next run. A re-import of an archived period does not replace the archived rows.
Archived rows hold shop, product, catalog item, category and customer ids, quantities and prices, never a
name, phone, email or address.

**What the app loses.** Every reader of sales_history sees only the months still in Atlas. Most use a window
well inside 18 months (the demand forecast, the owner and admin dashboards, "sold in the last 30 days") and
do not change. These have no date window, so after the first run their results drop the archived months:
- the customer dashboard's `shops_visited` (backend/app/api/routes/customer.py), a count of distinct shops
  over all of the customer's sales: a long-standing customer's count goes down;
- the customer dashboard's recommendations (customer.py, from the customer's 10 most recent purchases): a
  customer with no purchase in the kept months gets none;
- Apriori bundle training (backend/app/ai/recommend.py) reads every real sale: the bundle rules lose their
  older baskets and come from the kept months only;
- the "popular in category" fallback on a product page (backend/app/api/routes/catalog.py) sums quantity
  over all time: its ranking reflects the kept months only.
P8.3 does not change these (they are outside its files). A figure that must stay all-time needs its own
record (a counter or a summary kept in Atlas) built before the first production run; otherwise restore the
months into a scratch database (below) to compute it.

**Each run** writes a row to `archive_runs` in the database it archives: `_id` (the part id), `started_at`,
`finished_at`, `outcome` (running, succeeded or failed), `cutoff`, `months` (per month: `documents`,
`deleted`, `bytes`, `sha256`, `data_key`, `manifest_key`), and on failure `error`. The log ends with a JSON
line: the months archived, documents, deleted and bytes. On the 2nd of each month, check the job's last run
(Dashboard > the archive cron job > Events) and its archive_runs row; a failed run needs the steps under
Troubleshooting.

**Set-up.** Sync the Blueprint. For each archive cron job, enter the same NEARSHOP_BACKUP_BACKEND=s3,
NEARSHOP_BACKUP_S3_ENDPOINT, NEARSHOP_BACKUP_S3_BUCKET, NEARSHOP_BACKUP_S3_ACCESS_KEY_ID and
NEARSHOP_BACKUP_S3_SECRET_ACCESS_KEY as on the backup cron job of that environment (or a second R2 token on
the same backup bucket). Then, before the first run: `run_job 'uv run --no-dev python -m database.archive --plan'`
on the archive cron job, read-only, lists the eligible months and their rows.

**Look before and after.** `--plan` (read-only) lists the months the next run would archive;
`run_job 'uv run --no-dev python -m database.archive --list'` lists the archived parts in the bucket. After a
run, the admin storage card (P2.2) shows the drop in sales_history.

**Bring a month back.** Into a scratch database on the same cluster, never the database the API serves (the
tool refuses it, and the production name): every complete part of the month is downloaded and checked
against its manifest before anything is written, then inserted into that database's sales_history, skipping
_ids already there (a month archived in two parts may hold a row twice). Run it as a one-off job on the
archive cron job of the environment (`run_job`, with `$SERVICE_ID` that job's `crn-...` id):

```
run_job 'uv run --no-dev python -m database.archive --restore 2025-03 --target-db nearshop_sales_2025_03'
```

Run it again with another month and the same target to put several months side by side. The JSON summary
gives `parts`, `documents`, `inserted`, `skipped_duplicates` and `rows_in_target_month`; `inserted` plus
`skipped_duplicates` equals `documents`. A production archive restores only with NEARSHOP_ENVIRONMENT=production.
Exit 2 means refused, nothing written. If rows are needed back in the live sales_history (for example a tax
question about one shop), copy just those rows across by hand from the scratch database, keeping their _ids;
the next monthly run archives them again as a new part if they are still older than the cutoff. Drop the
scratch database when done (Atlas > Browse Collections > the database > Drop): it holds production data.

**Without the tool.** Each part is the mongodump format:
`mongorestore --uri <target uri> --gzip --db <scratch db> --collection sales_history <part id>.bson.gz`, after
checking the file's SHA-256 against its manifest (`shasum -a 256`).

**On a laptop:** `make backup`, then `make archive` (and `make archive ARCHIVE_ARGS=--plan`, `--list`, or
`'--restore 2025-03 --target-db nearshop_sales_2025_03'`), with the local database and BACKUP_DIR.

## Restore drill (quarterly, check 3.4)

First drill: plan step 1-B, by 22 Nov 2026 (R2 and R3). Then once a quarter, and after any change to the
backup or restore tools.

1. Check backup_runs on production: the newest row succeeded in the last 26 hours.
2. Note the start time, then run
   `run_job 'uv run --no-dev python -m database.restore --target-db nearshop_drill_<yyyymmdd>'` on
   nearshop-backup-production (the latest set).
3. In the job's log: exit 0 and the JSON summary. `total_seconds` is the tool's time, download included.
4. Note the end time when the counts are verified (exit 0 means every collection matched the manifest). The
   drill time is from step 2's start to here, and must be 2 hours or less.
5. Drop nearshop_drill_<yyyymmdd> (Atlas > Browse Collections > the database > Drop). It holds production
   data and must not stay.
6. Add a row below. A failed drill is an incident: fix the cause and repeat the drill within a week.

| Date | Set | Target database | Documents | Start to verified counts (minutes) | Tool total_seconds | Counts match | Passed (2 hours or less) | Run by | Notes |
|---|---|---|---|---|---|---|---|---|---|

## Troubleshooting

- **"NEARSHOP_BACKUP_BACKEND is off"** (exit 2): the cron job's backup settings are missing; set-up step 2.
- **Exit 1, "Backup failed"**: the log has the error, and backup_runs has a failed row with it. A bucket
  error leaves an incomplete set, deleted a day later. Fix it and Trigger Run.
- **A run takes longer than usual or the set grows fast**: check the sizes in the manifest; the largest
  collections are usually sales_history and text_embeddings. sales_history months older than 18 months are
  archived monthly (Archived sales history above); check archive_runs if it keeps growing.
- **"Archive refused ... no backup ... has succeeded" or "more than 26 hours ago"** (exit 2): the nightly
  backup is failing; fix it first (above), then Trigger Run on the archive cron job.
- **Exit 1, "Archive failed"**: archive_runs has a failed row with the error. The month that failed is the
  oldest eligible month not in the row's `months`. In `--list`, if that month has no part with the run's
  part id (the row's `_id`), or it shows INCOMPLETE, the run failed before the manifest and the month still
  has all its rows. If the part shows complete, the run failed while deleting: part of the month is now only
  in the archive (complete and verified), and the rest is still in Atlas. Do not re-import those rows or
  copy them back. Fix the cause
  (a bucket or a database error) and Trigger Run: finished months are not redone, and the rows left in a
  partly deleted month go in a new part of it.
- **"is not empty"**: restore into a new database name (one that does not exist yet, so no collection in
  it, not even an empty one), or drop the target first if it is a failed attempt.
- **"is at migration N but this code knows migrations up to M"**: the set comes from newer code. Run the
  restore on a service at the commit in the manifest (the `commit` field), or wait for that release.
- **The tool is unavailable** (the code cannot run): each file can also be loaded with mongorestore,
  `mongorestore --uri <target uri> --gzip --db <target db> --collection <name> <name>.bson.gz`, collection by
  collection (schema_migrations and counters too), into a database that does not exist yet. Check the counts
  against the manifest by hand, and only then run `python -m database.migrate` against the target: it applies
  migrations newer than the set and builds the code's validators and indexes. Never migrate the empty target
  first, or a newer unique or TTL index meets the old documents before their migration has run. Indexes the
  set has and the code no longer declares are not rebuilt this way; the manifest lists them.
