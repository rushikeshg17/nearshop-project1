# Monitoring, alerts and on-call

Every alert in plan section 8.10 that can exist before Phase 4 is listed here with its exact threshold,
the tool that raises it and who it reaches, and each is test-fired once and logged at the end. Scorecard
check 5.4; plan section 8.10 and manual step 2-A (R2, by 6 Dec 2026). The OTP-spend and WhatsApp alerts
are added at step 4-A, and the content security policy alert by P2.6.

Every alert goes to the person on call (rota below) by email and to the team Slack channel #nearshop-alerts.
Configure each tool's notification target once: Better Stack (Team > On-call calendar, or the alert
policy's escalation), Sentry (Alerts > each rule's action), Atlas (Project > Alerts > each alert's
notification: email to the on-call address and the Slack integration).

## Response targets

| Kind | Examples | Acknowledge and start work within |
|---|---|---|
| Outage | An uptime monitor down (home page, /api/health/ready through Vercel, /api/health direct), storage at 85%, connections above 80% | 15 minutes |
| Everything else | A new error issue, error rate, latency, storage at 70%, sweep heartbeat, backup heartbeat | 4 hours |

Within on-call hours (9:00 to 21:00 IST) the person on call acknowledges in the tool that raised the
alert, then posts in #nearshop-alerts what they found and did. An outage outside those hours is
picked up at 9:00 IST by that day's person, unless someone else is awake and takes it.

## Alerts

Environments: production is https://nearshop-umber.vercel.app (Vercel) and the nearshop-api Render service;
staging is the Vercel preview alias of main and nearshop-api-staging (docs/runbooks/release-and-rollback.md).
Every monitor and rule below exists once for each, named with the environment first
(production: home page). Production alerts go to the on-call person; staging alerts go to #nearshop-alerts only.

| # | Alert | Tool and where | Exact rule | Target |
|---|---|---|---|---|
| 1 | Home page down | Better Stack monitor, Status check | GET `https://<vercel-url>/` ; down when the status is not 2xx or 3xx or there is no answer in 30 s. Check every 3 minutes (the free plan's shortest; plan: 3 minutes or less), confirmation period 1 minute: alert after 2 consecutive failures (the failing check, then the failing re-check). Recovery after 2 passing checks. | Outage: 15 minutes |
| 2 | API not ready, through Vercel | Better Stack monitor, Status check | GET `https://<vercel-url>/api/health/ready` ; down on any status other than 2xx (the API answers 503 when the database or the migrations fail) or no answer in 30 s. Interval and confirmation as #1. | Outage: 15 minutes |
| 3 | API down, direct | Better Stack monitor, Status check | GET `https://<render-service>.onrender.com/api/health` ; down on any status other than 2xx or no answer in 30 s. Interval and confirmation as #1. Tells a Vercel problem (#2 down, #3 up) from an API problem (both down). | Outage: 15 minutes |
| 4 | Expiry sweep heartbeat older than 90 s | Better Stack monitor, Keyword check | GET `https://<vercel-url>/api/health/ready` ; down when the body does not contain the exact text `"sweep":true`. The API reports sweep true only while job_leases.sweep_expired.last_run_at is less than 90 s old (backend/app/main.py, readiness). Interval and confirmation as #1: a deploy's lease hand-over (up to 90 s) never alerts on its own. | 4 hours |
| 5 | Any new error issue | Sentry issue alert, project nearshop (backend and frontend events) | When a new issue is created, and when an issue changes from resolved to unresolved (a regression); environment = production (a copy of the rule with environment = staging posts to Slack only). Includes the storage job's 85% error (alert #10). | 4 hours |
| 6 | Error rate above 1% | Sentry metric alert, Transactions | failure_rate() above 1% over a 5-minute window, filter: event.type:transaction !transaction:/api/health* and transaction.status not one of not_found, unauthenticated, permission_denied, invalid_argument, already_exists, failed_precondition, resource_exhausted (4xx answers are the user's, not the server's). Resolve below 1%. | 4 hours |
| 7 | Search server p95 above 800 ms | Sentry metric alert, Transactions | p95(transaction.duration) above 800 ms over a 10-minute window, filter: transaction:/api/search. Resolve below 600 ms (the target, plan section 8.11). Search requests are sampled at 25% (NEARSHOP_SENTRY_SEARCH_TRACES_SAMPLE_RATE). | 4 hours |
| 8 | Any route p95 above 1,500 ms | Sentry metric alert, Transactions | p95(transaction.duration) above 1,500 ms over a 10-minute window, one rule per route of the load-test mix (plan section 8.11): /api/search, /api/search/suggest, /api/products/{product_id}, /api/owner/overview, /api/reservations and /api/reservations/{reservation_id}/cancel; plus one catch-all over every transaction except /api/health*. Use the route templates exactly as Sentry's Performance page lists them (transaction_style url). | 4 hours |
| 9 | Atlas storage at 70% and at 85% | Atlas project alert, condition Logical Size (the only storage condition on M0 and Flex) | Two alerts per cluster. Staging (M0, 512 MB): Logical Size above 358 MB, and above 435 MB. Production (Flex, 5 GB): above 3.5 GB, and above 4.25 GB. While production is still on M0 it uses the M0 values. Logical Size counts the uncompressed documents plus indexes of every database on the cluster, as the tier limit does. | 70%: 4 hours. 85%: outage, 15 minutes |
| 10 | Storage at 70% and 85%, in-app backstop | NearShop's daily storage snapshot (backend/app/services/analytics.py, job snapshot_storage) | When the day's snapshot is taken (the first scheduler tick of each IST day), it logs a warning at 70% and an error at 85% of NEARSHOP_STORAGE_LIMIT_MB (render.yaml: 512 staging, 5120 production). The error becomes a Sentry issue, so alert #5 carries it. It counts only the NearShop database. The admin overview's Database storage card shows the same figures and the projected date of reaching 85%. | As #9 |
| 11 | Atlas connections above 80% | Atlas project alert, condition Connections | Connections above 400 (80% of the 500 that M0 and Flex allow), for staging and production clusters. | Outage: 15 minutes |
| 12 | No successful backup for more than 26 hours | Better Stack heartbeat, one per environment (nearshop-backup-staging, nearshop-backup-production) | Period 24 hours, grace 2 hours: alert when no ping has arrived for 26 hours. The backup cron job pings the heartbeat URL (NEARSHOP_BACKUP_HEARTBEAT_URL, set in the Render dashboard) only after python -m database.backup exits 0 (render.yaml startCommand), that is after a succeeded row is written to backup_runs. A failed run, a run that never starts and a cron job that is gone all leave the heartbeat silent. | 4 hours |
| 13 | OTP spend above 80% of the daily cap | **Pending: P4.1** | Configured and test-fired when P4.1 reaches production (plan step 4-A, by 14 Feb 2027). | 4 hours |
| 14 | WhatsApp failure rate above 5% over 1 hour | **Pending: P4.3** | Configured and test-fired when P4.3 reaches production (plan step 4-A). | 4 hours |
| 15 | New content security policy violation from the site's own pages | **Added by P2.6** | Sentry security reports, after enforcement (plan section 8.13). | 4 hours |

Better Stack's free plan has 10 monitors: #1 to #4 for each environment use 8. The two heartbeats (#12)
count separately.

If Sentry's plan does not include metric alerts (#6 to #8), record that in the test-fire log and raise it
with R2: those three alerts are then not met, and scorecard check 5.4 stays failed until they are.

## Setting up (step 2-A)

1. **Better Stack.** Create monitors #1 to #4 for staging and production, with the rules above. Create
   the two heartbeats (#12) and copy each heartbeat's URL into its Render cron job's
   NEARSHOP_BACKUP_HEARTBEAT_URL (Render > the cron job > Environment). Until it is set the job does not
   ping, and the heartbeat alerts after 26 hours. Trigger one run of each cron job (Render > Trigger Run)
   and check the heartbeat turns green.
2. **Sentry.** With NEARSHOP_SENTRY_DSN set on both Render services (P2.1), create rules #5 to #8 for
   production, and staging copies that post to Slack only.
3. **Atlas.** In each project (staging and production), Project > Alerts > Add: alerts #9 and #11, with
   the values for that cluster's tier. When production moves from M0 to Flex, change both storage values
   and NEARSHOP_STORAGE_LIMIT_MB together.
4. **Rota.** Fill in the rota below and publish it in #nearshop-alerts.
5. **Test-fire** every alert once (next section) and log each one.

## Test-firing

Fire each alert on staging where it can be, and on production only where staging cannot show it. Each
test proves the alert reaches the on-call person by email and Slack. Restore every changed threshold or
setting straight after, and say so in the log's notes.

| # | How to fire it | Expect |
|---|---|---|
| 1, 2, 3 | Render > nearshop-api-staging > Suspend (staging stopped). For #1, also pause the staging Vercel deployment or point the monitor at a URL that returns 404 for the test. Note the time, wait for the alert, then Resume. | #2 and #3 alert within 5 minutes of stopping staging (3-minute interval plus 1-minute confirmation), then recover. Acceptance: the uptime alert reaches the on-call person within 5 minutes. |
| 4 | Set NEARSHOP_ENABLE_SCHEDULER=false on nearshop-api-staging and redeploy: no process runs the sweep, so its heartbeat ages past 90 s. Set it back to true afterwards. | /api/health/ready shows `"sweep":false` and status degraded; the keyword monitor alerts within about 5 minutes. |
| 5 | On the staging service, Render > Shell: `uv run --no-dev python -c "import os, sentry_sdk; sentry_sdk.init(dsn=os.environ['NEARSHOP_SENTRY_DSN'], environment='staging'); sentry_sdk.capture_message('P2.2 test-fire', level='error'); sentry_sdk.flush()"` | A new issue named P2.2 test-fire, and the staging rule's Slack message. Resolve the issue. Then repeat once with `environment='production'` from the same shell, to fire the production rule to the on-call person; resolve it. |
| 6 | Temporarily lower the staging copy's threshold to above 0% and remove the status filter, then open a few product pages that do not exist (404, not_found) on staging. | Alert within about 5 minutes. Restore the rule. |
| 7, 8 | Temporarily lower each staging copy's threshold to above 1 ms, then run the full staging smoke test (make smoke with SMOKE_MODE=full and the staging URL). | Each rule alerts within about 10 minutes. Restore the rules. |
| 9 | In the staging project, temporarily change each storage alert's value to 1 MB. | Atlas sends both storage alerts. Restore 358 MB and 435 MB. |
| 10 | On the staging service, set NEARSHOP_STORAGE_LIMIT_MB to a value below the database's current size (the admin storage card shows it), redeploy, and wait for the next IST day's snapshot (00:00 IST). Set it back to 512. | The snapshot logs the 85% error: a new Sentry issue and alert #5. |
| 11 | In the staging project, temporarily change the connections alert's value to 1. | Atlas sends the alert (the API holds more than one connection). Restore 400. |
| 12 | In Better Stack, temporarily set the staging heartbeat's period and grace to their minimums. Set them back to 24 hours and 2 hours, then Render > nearshop-backup-staging > Trigger Run. | The heartbeat alerts, then turns green after the triggered run. |

## On-call rota

One person per day, 9:00 to 21:00 IST. Swap by editing this table in a pull request and posting in
#nearshop-alerts. R2 keeps it current.

| Day | On call, 9:00 to 21:00 IST | Back-up |
|---|---|---|
| Monday | (to be filled at step 2-A) | |
| Tuesday | | |
| Wednesday | | |
| Thursday | | |
| Friday | | |
| Saturday | | |
| Sunday | | |

## When an alert fires

| Alert | First look | Usual fix |
|---|---|---|
| Uptime (#1 to #3) | Render > the service > Events and Logs; Vercel > Deployments; /api/health/db | Roll back a bad release (release-and-rollback.md); restart; check Atlas status |
| Sweep (#4) | /api/health/ready; job_leases in Atlas (sweep_expired: holder, last_run_at, last_result) | A crashed or stuck API: restart it. NEARSHOP_ENABLE_SCHEDULER must be true |
| Errors and latency (#5 to #8) | The Sentry issue or trace; the X-Request-Id in the JSON request logs | Fix forward or roll back |
| Storage (#9, #10) | Admin overview > Database storage: the largest collections and the projected date | Move up a tier (M0 to Flex), or archive (P8.3). Never delete production data to make room |
| Connections (#11) | Atlas > the cluster > Metrics > Connections | A connection leak after a release: roll back |
| Backup (#12) | Render > the backup cron job > its last run's log; backup_runs in Atlas | docs/runbooks/restore.md |

## Test-fire log

One row per alert and environment. Date in IST. Minutes: from firing to the alert reaching the on-call person.

| # | Alert | Environment | Date | Fired by | Reached (email, Slack) | Minutes | Notes |
|---|---|---|---|---|---|---|---|
| 1 | Home page down | staging | not yet | | | | |
| 2 | API not ready, through Vercel | staging | not yet | | | | |
| 3 | API down, direct | staging | not yet | | | | |
| 4 | Sweep heartbeat | staging | not yet | | | | |
| 5 | New error issue | staging and production | not yet | | | | |
| 6 | Error rate above 1% | staging | not yet | | | | |
| 7 | Search p95 above 800 ms | staging | not yet | | | | |
| 8 | Route p95 above 1,500 ms | staging | not yet | | | | |
| 9 | Atlas storage 70% and 85% | staging | not yet | | | | |
| 10 | Storage snapshot 85% | staging | not yet | | | | |
| 11 | Atlas connections above 80% | staging | not yet | | | | |
| 12 | Backup heartbeat | staging | not yet | | | | |
| 13 | OTP spend (P4.1) | production | pending, step 4-A | | | | |
| 14 | WhatsApp failures (P4.3) | production | pending, step 4-A | | | | |
