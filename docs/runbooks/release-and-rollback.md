# Release and rollback

Every change reaches production through staging, and a bad release can be rolled back in 10 minutes or
less from the decision to the previous version serving traffic. Scorecard checks 4.1 (always-on
production), 5.2 (staging first, release log) and 5.5 (rollback rehearsal); plan section 8.12.

## Environments

| | Staging | Production |
|---|---|---|
| Git branch | main | release |
| Render service (render.yaml) | nearshop-api-staging, Starter (0.5 CPU, 512 MB) | nearshop-api, Standard (1 CPU, 2 GB), always on |
| Region | singapore | singapore |
| NEARSHOP_ENVIRONMENT | staging | production |
| Database | the staging Atlas cluster (M0) | the production Atlas cluster (Flex) |
| Vercel | preview deployment of main (its branch alias) | production deployment of release, https://nearshop-umber.vercel.app |
| Smoke test | full mode, automatic after each push to main | read-only mode, by hand after each release |

Both services deploy only after a commit's GitHub checks pass (autoDeployTrigger: checksPass) and run
exactly one uvicorn worker: the scheduler and the search index live in each API process until P1.7 and
P8.1. /api/health reports the deployed commit (RENDER_GIT_COMMIT), so anyone can see which version an
environment serves: open https://<vercel-url>/api/health.

Secrets live only in the Render dashboard, per service: NEARSHOP_MONGODB_URI, NEARSHOP_SECRET_KEY and
NEARSHOP_PROTECTED_HOSTS (render.yaml marks them sync: false). Staging and production never share one.
No laptop holds either environment's credentials.

## One-time set-up (plan step 1-A, R2)

1. **Release branch first.** Create release at the current main commit before syncing the Blueprint, so
   the production service has a branch to track: `git push origin origin/main:refs/heads/release`.
   Protect release in GitHub (Settings > Branches): no force pushes, no deletion, and only R2 and R3 may
   push to it.
2. **Render.** Sync the Blueprint (render.yaml). For nearshop-api-staging, enter the staging cluster's URI,
   a new secret key (`python3 -c "import secrets;print(secrets.token_urlsafe(48))"`) and the protected
   hosts (the staging and production Atlas project domains, comma-separated). nearshop-api keeps the values
   it already has; add NEARSHOP_PROTECTED_HOSTS to it. The Blueprint moves nearshop-api from the free plan
   to Standard and from main to release.
3. **Vercel** (Pro plan, project settings):
   - Git > Production Branch: release.
   - Environment Variables: NEARSHOP_API_ORIGIN for Production is the production Render URL
     (nearshop-api); for Preview it is the staging Render URL (nearshop-api-staging). Redeploy both after
     changing them.
   - Staging's URL is the Vercel branch alias of main's preview deployment
     (https://<project>-git-main-<team>.vercel.app). If Deployment Protection covers previews, create a
     Protection Bypass for Automation secret and store it in GitHub as NEARSHOP_SMOKE_VERCEL_BYPASS.
4. **GitHub Actions** (Settings > Secrets and variables > Actions):
   - variables NEARSHOP_STAGING_URL (staging's Vercel URL) and NEARSHOP_PRODUCTION_URL;
   - secrets NEARSHOP_SMOKE_EMAIL and NEARSHOP_SMOKE_PASSWORD (the staging smoke account).
5. **Staging data and smoke account.** Staging holds generated data only, never a copy of production users.
   Load the generated city dataset into the staging cluster once (with the P1.6 tools when they exist).
   Create the smoke account as a customer on staging: set NEARSHOP_SIGNUPS_OPEN to true on
   nearshop-api-staging, register through the staging site with a random password of 24 or more characters,
   set it back to false, and store the email and password only in the GitHub secrets above.

## Release flow

1. Merge to main after CI passes.
2. Render deploys main to staging. The Smoke workflow (.github/workflows/smoke.yml) starts when CI finishes
   for the push, waits up to 10 minutes for staging's /api/health to report the commit, then runs full mode:
   health, meta, three fixed searches, sign-in, reserve and cancel. It prints each step's time.
3. **Promotion.** Fast-forward release to a main commit whose staging smoke passed at least 1 hour
   earlier. Check the run in Actions > Smoke (its head commit and finish time), then:

   ```
   git fetch origin
   git merge-base --is-ancestor origin/release <commit> && git push origin <commit>:refs/heads/release
   ```

   The push is a fast-forward or git refuses it; never force-push release. CI does not run again for
   release: the commit already carries main's passing checks (CI and Smoke), so Render deploys it to
   production. A commit whose smoke failed is held back by checksPass as well.
4. When nearshop-api reports the new commit on /api/health, run Actions > Smoke > Run workflow with target
   production (read-only), or `make smoke` locally.
5. Add a row to the release log below. Every production release gets one (check 5.2).

Migrations run as Render's pre-deploy step once P1.4 adds them.

If the Smoke run fails only at "wait for commit", staging did not deploy within 10 minutes: check the
deploy in Render (build failure, or still waiting for checks), then re-run the workflow once it is live.
The workflow starts from CI's completion rather than from the push because checksPass waits for every
check on a commit; should Render ever show staging waiting on the Smoke check itself, tell R2, who
re-runs Smoke after the deploy and raises it with Render support.

## Expand, then contract

A release may only add: new fields, new indexes, new enum values, new collections. Removing or renaming a
field, an index or an enum value, or tightening a validator, happens at least one release later, after no
deployed version reads or writes the old form. Code must accept documents with and without a new field.
This keeps the database compatible with the previous release, so a rollback never needs a data change. A
pull request that breaks the rule is not merged.

## Rollback

**Who decides.** The tech lead (R2) or, when R2 cannot be reached within 5 minutes, the backend engineer
(R3) or the team lead (R1). Whoever is on call may roll back straight away when production is down or is
losing or exposing data, and tells the others at once. Fixing forward is never a reason to delay a
rollback of a broken release.

Target: 10 minutes or less from the decision to the previous version serving traffic. Start the clock at
the decision and write the times down as you go.

1. **API (Render).** Dashboard > nearshop-api > Events: pick the last good deploy > Rollback. A dashboard
   rollback turns auto-deploy off for the service.
2. **Frontend (Vercel), when the frontend changed in the bad release.** Project > Production Deployment >
   Instant Rollback, and choose the last good deployment. Vercel then stops assigning new release pushes
   to the production domain.
   Steps 1 and 2 can run at the same time by two people.
3. **Check.** https://nearshop-umber.vercel.app/api/health reports the old commit, and the production smoke
   (read-only) passes. Stop the clock.
4. **Fix forward.** Fix on main and let it pass staging. release keeps its history: never reset or
   force-push it to undo the bad commit.
5. **Restore automatic deploys, then promote the fix.** Turn Render's auto-deploy back on first
   (nearshop-api > Settings > Auto-Deploy: After CI checks pass), then fast-forward release to the fix as
   usual, so Render deploys it. Once Vercel has built the fix, Vercel > Undo Rollback and promote the
   fixed deployment, which turns Vercel's automatic production assignment back on.
6. Add a row to the release log for the rollback and the fix, and write up the incident.

## Release log

One row per production release (check 5.2), including rollbacks. Staging smoke result: the Smoke run's
outcome and link for that commit, with its finish time.

| Date | Commit | Staging smoke result | Promoted by |
|---|---|---|---|

## Rollback rehearsal log

Rehearse on production at least once before G1 (plan step 1-B, by 22 Nov 2026, R2 and R3) and then once a
quarter: roll back to the previous release, time it, then restore the current release. Check 5.5 needs a
time of 10 minutes or less.

| Date | From commit | To commit | Decision to old version serving (minutes) | Passed (10 min or less) | Run by | Notes |
|---|---|---|---|---|---|---|
