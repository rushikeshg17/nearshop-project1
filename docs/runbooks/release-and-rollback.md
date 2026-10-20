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
| Smoke test | full mode, automatic within about 10 minutes of each deploy | read-only mode, by hand after each release |

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

   The scheduled staging smoke starts once NEARSHOP_STAGING_URL is set. Set it last, after step 5:
   until staging has data and the smoke account exists, every staging smoke fails (meta, search or
   sign-in) and records a failure on each commit it tests. Those failures are expected; promote nothing
   until a commit passes.
5. **Staging data and smoke account.** Staging holds generated data only, never a copy of production users.
   Load the generated city dataset (the seed, NEARSHOP_SEED_CITY) into the staging cluster once, from
   Render, so the staging URI never reaches a laptop. The P1.6 tools will replace this step.
   - The seed refuses staging as configured, on purpose: staging's NEARSHOP_PROTECTED_HOSTS lists the
     staging cluster, and the seed runs only with NEARSHOP_ENVIRONMENT demo. For the one seed command
     below, both are overridden, and NEARSHOP_PROTECTED_HOSTS lists the production project domain only,
     so production stays refused. Never leave it empty. This needs staging and production in separate
     Atlas projects (plan step 1-A); if they share one, stop and wait for P1.6.
   - The seed needs about 1 GB of memory (measured: 0.95 GB peak for ballari) and Starter has 512 MB,
     so first set nearshop-api-staging > Settings > Instance Type to Standard (2 GB) and wait for that
     deploy to go live.
   - Dashboard > nearshop-api-staging > Shell, then (in backend; `cd /opt/render/project/src/backend`
     if the shell starts elsewhere), with the production project domain in place of the placeholder:

     ```
     NEARSHOP_ENVIRONMENT=demo NEARSHOP_PROTECTED_HOSTS=<production-project-domain> \
     NEARSHOP_DEMO_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
       uv run --no-dev python -m database.seed.seed --confirm-db nearshop
     ```

     The seeded demo accounts get that random password, which nobody keeps; on staging only the smoke
     account below signs in. The seed resets the staging database, so run it before creating the smoke
     account, and run it again only together with re-creating that account.
   - Set the instance type back to Starter (a later Blueprint sync would also set it back).

   Then create the smoke account as a customer on staging: set NEARSHOP_SIGNUPS_OPEN to true on
   nearshop-api-staging, register through the staging site with a random password of 24 or more characters,
   set it back to false, and store the email and password only in the GitHub secrets above. Finally set
   NEARSHOP_STAGING_URL (step 4) and check that the next Smoke run passes.

## Release flow

1. Merge to main after CI passes.
2. Render deploys main to staging once CI passes. Every 10 minutes the Smoke workflow
   (.github/workflows/smoke.yml) reads the commit staging serves from /api/health; when that commit has no
   result yet, it runs full mode against it (health, meta, three fixed searches, sign-in, reserve and
   cancel, then checks staging still serves the same commit), prints each step's time, and records the
   result as the commit status "Smoke / staging" on that commit, linked to the run. GitHub can start
   scheduled runs late, so allow up to about 20 minutes after the deploy.
3. **Promotion.** Fast-forward release to a main commit whose "Smoke / staging" status is success and at
   least 1 hour old. The status, not a Smoke run's colour, is the result: a run reports on whatever
   commit staging served, which is not always the run's own commit. Check it, then push:

   ```
   git fetch origin
   gh api "repos/{owner}/{repo}/commits/<commit>/statuses" \
     --jq '[.[] | select(.context == "Smoke / staging")][0] | "\(.state) \(.updated_at) \(.target_url)"'
   # must print: success <time at least 1 hour ago> <run link>
   git merge-base --is-ancestor origin/release <commit> && git push origin <commit>:refs/heads/release
   ```

   The push is a fast-forward or git refuses it; never force-push release. CI does not run again for
   release: Render's checksPass sees the checks already on the commit from main (CI, and any Smoke run
   started while it was main's newest commit, which fails only if that commit's own smoke failed), so
   Render deploys it to production. Render's docs say checksPass reads GitHub checks; they do not say
   it reads commit statuses, so do not count on Render to stop a commit whose smoke failed. The status
   check above is the gate.
4. When nearshop-api reports the new commit on /api/health, run Actions > Smoke > Run workflow with
   "Use workflow from: release" and target production (read-only), or `make smoke` locally.
5. Add a row to the release log below. Every production release gets one (check 5.2).

Migrations run as Render's pre-deploy step once P1.4 adds them.

**Why staging smoke is a sweep, not a run per push.** checksPass deploys a commit only after the GitHub
checks on it pass, and every Actions run, whatever starts it, is a check on a main commit, usually the
newest. A run that waited for the newest commit to reach staging could hold up that very deploy, time
out, and then block the commit for good. So no Smoke run ever waits for a deploy, none is ever cancelled
(no concurrency group), and a run fails only when the commit it tested is its own commit, which by then
is already live on staging. A failure that belongs to another commit goes to that commit's status and
to the run summary, and the run passes.

**When a commit has no "Smoke / staging" status.**
- Pushes close together: Render may deploy only the newest, and a commit staging never served is never
  tested. Promote a later commit that passed instead; it contains the earlier one.
- Staging deployed again during a run: the result belonged to neither commit, so none was recorded, and
  the next run tests the new commit.
- Otherwise open the latest Smoke runs (Actions > Smoke): the summary says why nothing was tested
  (staging unreachable, /api/health reporting no commit). Check the deploy in Render (build failure, or
  still waiting for checks), fix it, and wait for the next run or start one by hand.
- To test again a commit that already has a result (for example after fixing staging's data), run
  Actions > Smoke > Run workflow with target staging; it tests whatever staging serves and records a new
  status.
- GitHub turns scheduled workflows off after 60 days without repository activity; Actions > Smoke then
  shows a button to turn it back on.

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

One row per production release (check 5.2), including rollbacks. Staging smoke result: the commit's
"Smoke / staging" status, its time and its run link (the command in Release flow step 3 prints them).

| Date | Commit | Staging smoke result | Promoted by |
|---|---|---|---|

## Rollback rehearsal log

Rehearse on production at least once before G1 (plan step 1-B, by 22 Nov 2026, R2 and R3) and then once a
quarter: roll back to the previous release, time it, then restore the current release. Check 5.5 needs a
time of 10 minutes or less.

| Date | From commit | To commit | Decision to old version serving (minutes) | Passed (10 min or less) | Run by | Notes |
|---|---|---|---|---|---|---|
