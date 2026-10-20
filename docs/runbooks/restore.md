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
26 hours old (plan section 8.10; wired up in P2.2). To look by hand: Atlas > the cluster > Browse
Collections > nearshop > backup_runs, sorted by `started_at` descending.

## One-time set-up (plan step 1-A, R2)

1. **Buckets.** In Cloudflare R2 create a private bucket for production backups and one for staging
   backups (no public access, no custom domain), and an API token for each, Object Read & Write on that
   bucket only. Separate buckets keep staging's token from reading production's customers; if the team
   keeps a single backup bucket, the prefixes still separate the sets, but staging's token can then read
   production's sets, so give staging its own bucket before production holds real customer data.
2. **Render.** Sync the Blueprint (render.yaml). It creates nearshop-backup-staging (branch main) and
   nearshop-backup-production (branch release). Each reads NEARSHOP_MONGODB_URI from its API service. For
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
- a target that holds any document;
- the production database name (NEARSHOP_PRODUCTION_MONGODB_DB, and in production NEARSHOP_MONGODB_DB, the
  database the API serves), unless `--emergency` is given and the operator types the name when asked;
- a production set anywhere but in production (NEARSHOP_ENVIRONMENT=production);
- a file whose size or SHA-256 differs from the manifest, and a set whose migration is newer than the code.

It then restores the set's schema (the migration runner brings the empty target to the set's migration),
inserts the documents in batches, restores counters, checks every collection's count against the manifest
(any difference: exit 1, "Restore FAILED"), applies any newer migrations, and prints a JSON summary with
`total_seconds`. Exit 2 means refused, nothing written.

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
  collections are usually sales_history and text_embeddings (P8.3 archives old sales).
- **"is not empty"**: restore into a new database name, or drop the target first if it is a failed attempt.
- **"is at migration N but this code knows migrations up to M"**: the set comes from newer code. Run the
  restore on a service at the commit in the manifest (the `commit` field), or wait for that release.
- **The tool is unavailable** (the code cannot run): each file can also be loaded with mongorestore,
  `mongorestore --uri <target uri> --gzip --db <target db> --collection <name> <name>.bson.gz`, collection by
  collection, after creating the schema with `python -m database.migrate` against the target. Check the counts
  against the manifest by hand.
