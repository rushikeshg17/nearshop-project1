.PHONY: setup db-up db-down schema migrate seed backup restore api web test retrain lint lint-backend lint-frontend \
	build audit audit-waived-ghsas secret-scan ci smoke

# The local MongoDB from docker-compose.yml: a single-node replica set on localhost:27017.
LOCAL_MONGODB_URI ?= mongodb://localhost:27017/?replicaSet=rs0
LOCAL_MONGODB_DB ?= nearshop
# The test suite reads only this URI and refuses anything but a local server. It reaches the same
# local server as LOCAL_MONGODB_URI, written differently because the suite also refuses a URI equal
# to NEARSHOP_MONGODB_URI; each run creates and drops its own nearshop_test_<random> database.
NEARSHOP_TEST_MONGODB_URI ?= mongodb://127.0.0.1:27017/?directConnection=true
# How db-up starts MongoDB. Where MongoDB is already running (CI starts it with make db-up in its own
# step first), run make test DB_UP=
DB_UP ?= docker compose up -d --wait mongo
# make build only needs an API origin for the proxy rewrites it writes into the build; CI uses a dummy.
NEARSHOP_API_ORIGIN ?= http://api.invalid
# make smoke: the smoke test (backend/scripts/smoke.py) against a deployed environment's Vercel URL.
# Read-only by default (GET requests only, safe against production). SMOKE_MODE=full also signs in and
# reserves and cancels an item, so run it only against staging, with NEARSHOP_SMOKE_EMAIL and
# NEARSHOP_SMOKE_PASSWORD in the environment (never in backend/.env).
SMOKE_URL ?= https://nearshop-umber.vercel.app
SMOKE_MODE ?= read-only
# The audit reads the waivers file with this exact PyYAML, in CI and locally alike.
PYYAML_VERSION := 6.0.3
WAIVERS_FILE := .github/dependency-waivers.yml

setup:
	cd backend && uv sync && ( [ -f .env ] || cp .env.example .env )
	cd frontend && npm install

db-up:
	$(DB_UP)

db-down:
	docker compose down

# Applies pending schema migrations, then creates missing collections, validators and indexes
# (backend/database/migrate.py). Always the local database, whatever backend/.env points at: staging and
# production migrate in their Render pre-deploy step. MIGRATE_ARGS=--status only reports.
MIGRATE_ARGS ?=
schema: migrate  # the old name; it used to run ensure_schema against whatever backend/.env named
migrate: db-up
	cd backend && NEARSHOP_MONGODB_URI='$(LOCAL_MONGODB_URI)' NEARSHOP_MONGODB_DB='$(LOCAL_MONGODB_DB)' \
		uv run python -m database.migrate $(MIGRATE_ARGS)

# Always seeds the local database, whatever backend/.env points at. To seed a demo cluster, run the seed
# module yourself with NEARSHOP_ENVIRONMENT=demo and --confirm-db (see README).
seed: db-up
	cd backend && NEARSHOP_MONGODB_URI='$(LOCAL_MONGODB_URI)' NEARSHOP_MONGODB_DB='$(LOCAL_MONGODB_DB)' \
		uv run python -m database.seed.seed --confirm-db '$(LOCAL_MONGODB_DB)'

# Off-site backups (backend/database/backup.py, backend/database/restore.py; docs/runbooks/restore.md).
# Like migrate and seed, both always use the local database, whatever backend/.env points at, with a local
# directory, BACKUP_DIR (outside the repository), as the store; the tools' own safeguards apply as they
# are. Staging and production back up in their Render cron jobs and restore in one-off jobs there.
#   make backup                                    # back up the local database, then delete expired sets
#   make backup BACKUP_ARGS=--list                 # list the sets
#   make restore TARGET_DB=nearshop_restored       # restore the latest set into an empty database
#   make restore TARGET_DB=nearshop_restored BACKUP_SET=2026-10-21T213000Z
# RESTORE_ARGS passes further flags (--list, or --emergency to restore into the production database name
# after typing it).
BACKUP_DIR ?= $(HOME)/.nearshop/backups
BACKUP_ARGS ?=
TARGET_DB ?=
BACKUP_SET ?= latest
RESTORE_ARGS ?=
LOCAL_BACKUP_ENV = NEARSHOP_MONGODB_URI='$(LOCAL_MONGODB_URI)' NEARSHOP_MONGODB_DB='$(LOCAL_MONGODB_DB)' \
	NEARSHOP_BACKUP_BACKEND=local NEARSHOP_BACKUP_DIR='$(BACKUP_DIR)'

backup: db-up
	cd backend && $(LOCAL_BACKUP_ENV) uv run python -m database.backup $(BACKUP_ARGS)

restore: db-up
	cd backend && $(LOCAL_BACKUP_ENV) uv run python -m database.restore --target-db '$(TARGET_DB)' \
		--set '$(BACKUP_SET)' $(RESTORE_ARGS)

api:
	cd backend && uv run uvicorn app.main:app --reload --reload-dir app --port 8000

web:
	cd frontend && npm run dev

test: db-up
	cd backend && NEARSHOP_TEST_MONGODB_URI='$(NEARSHOP_TEST_MONGODB_URI)' uv run pytest -q

retrain:
	cd backend && uv run python -m app.ai.pipeline

smoke:
	cd backend && uv run python scripts/smoke.py '$(SMOKE_URL)' --mode '$(SMOKE_MODE)'

lint: lint-backend lint-frontend

lint-backend:
	cd backend && uv run ruff check app database scripts tests

lint-frontend:
	cd frontend && npx tsc --noEmit && npx eslint src

# The frontend production build.
build:
	cd frontend && NEXT_TELEMETRY_DISABLED=1 NEARSHOP_API_ORIGIN='$(NEARSHOP_API_ORIGIN)' npm run build

# Dependency audit: high and critical advisories in the frontend's production dependencies and in
# backend/uv.lock, each failing unless waived in .github/dependency-waivers.yml (see that file).
AUDIT_PYTHON = uv run --quiet --python 3.12 --no-project --with 'pyyaml==$(PYYAML_VERSION)' python -c "$$NEARSHOP_AUDIT_PY"

audit:
	$(AUDIT_PYTHON) audit '$(WAIVERS_FILE)'

# The waived GHSA ids, comma-separated, for GitHub's dependency review in CI.
audit-waived-ghsas:
	@$(AUDIT_PYTHON) ghsas '$(WAIVERS_FILE)'

# Secret scan of the full git history (every commit on every ref), with the rules in .gitleaks.toml.
# Needs gitleaks 8.25 or newer (CI uses 8.30.1). It never prints a secret it finds.
secret-scan:
	@command -v gitleaks >/dev/null 2>&1 || { echo "gitleaks is not installed (brew install gitleaks, or see https://github.com/gitleaks/gitleaks#installing)." >&2; exit 1; }
	gitleaks git --config .gitleaks.toml --redact --verbose --no-banner --exit-code 1 .

# The same checks as .github/workflows/ci.yml, except GitHub's dependency review (pull requests only).
ci: lint test build secret-scan audit

# The audit program behind make audit. It reads the waivers, runs npm audit (frontend production
# dependencies) and uv audit (backend/uv.lock), and fails on any high or critical advisory without a
# current waiver, and on any expired, too-long or incomplete waiver. uv audit reports no severity, so
# each Python advisory's severity comes from its GitHub advisory record in OSV; an advisory whose
# severity cannot be found counts as high. With "ghsas" instead of "audit", it prints the waived GHSA
# ids, comma-separated, for GitHub's dependency review.
define NEARSHOP_AUDIT_PY
import datetime
import json
import subprocess
import sys
import urllib.request

import yaml

BLOCKING = {"HIGH", "CRITICAL"}
MAX_DAYS = 30
mode, waivers_path = sys.argv[1], sys.argv[2]
today = datetime.datetime.now(datetime.UTC).date()
problems = []


def load_waivers():
    with open(waivers_path) as f:
        data = yaml.safe_load(f) or {}
    current = {}
    for n, w in enumerate(data.get("waivers") or [], 1):
        missing = [k for k in ("advisory", "package", "reason", "expires") if not w.get(k)]
        if missing:
            problems.append(f"waiver {n} in {waivers_path} is missing: {', '.join(missing)}")
            continue
        expires = w["expires"]
        if not isinstance(expires, datetime.date):
            try:
                expires = datetime.date.fromisoformat(str(expires))
            except ValueError:
                problems.append(f"waiver {n} ({w['advisory']}): expires is not a YYYY-MM-DD date")
                continue
        if expires < today:
            problems.append(f"waiver {n} ({w['advisory']}, {w['package']}) expired on {expires}")
        elif (expires - today).days > MAX_DAYS:
            problems.append(f"waiver {n} ({w['advisory']}, {w['package']}) expires {expires}, "
                            f"more than {MAX_DAYS} days ahead")
        else:
            current[(str(w["advisory"]).upper(), str(w["package"]).lower())] = (expires, str(w["advisory"]))
    return current


def run_json(cmd, cwd):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(out.stderr)
        raise SystemExit(f"{' '.join(cmd)} did not return a JSON report (exit {out.returncode})")


def npm_findings():
    report = run_json(["npm", "audit", "--omit=dev", "--json"], "frontend")
    if "error" in report:
        raise SystemExit(f"npm audit failed: {report['error']}")
    found = {}
    for vuln in report.get("vulnerabilities", {}).values():
        for via in vuln.get("via", []):
            if isinstance(via, dict):  # a string names another package; its advisories are listed there
                advisory = str(via.get("url", "")).rstrip("/").rsplit("/", 1)[-1] or str(via.get("source"))
                found[(advisory, via["name"])] = (str(via.get("severity", "unknown")).upper(), [advisory])
    return found


def osv_severity(ghsa):
    try:
        with urllib.request.urlopen(f"https://api.osv.dev/v1/vulns/{ghsa}", timeout=30) as r:
            return str(json.load(r).get("database_specific", {}).get("severity") or "UNKNOWN").upper()
    except OSError:
        return "UNKNOWN"


def uv_findings():
    report = run_json(["uv", "audit", "--frozen", "--preview-features", "audit-command,json-output",
                       "--output-format", "json"], "backend")
    found = {}
    for vuln in report.get("vulnerabilities", []):
        ids = [vuln["id"], *vuln.get("aliases", [])]
        ghsa = next((i for i in ids if i.startswith("GHSA-")), None)
        key = (ghsa or vuln["id"], vuln["dependency"]["name"])
        if key not in found:
            found[key] = (osv_severity(ghsa) if ghsa else "UNKNOWN", ids)
        else:
            found[key] = (found[key][0], found[key][1] + ids)
    return found


waivers = load_waivers()
if mode == "ghsas":
    print(",".join(sorted({orig for _, orig in waivers.values() if orig.upper().startswith("GHSA-")})))
    raise SystemExit(0)

blocking = 0
for ecosystem, findings in (("npm", npm_findings()), ("pypi", uv_findings())):
    print(f"{ecosystem}: {len(findings)} advisories")
    for (advisory, package), (severity, ids) in sorted(findings.items()):
        counts = severity in BLOCKING or severity == "UNKNOWN"
        waived = next((waivers[(i.upper(), package.lower())][0] for i in ids
                       if (i.upper(), package.lower()) in waivers), None)
        if not counts:
            status = "below high, not blocking"
        elif waived:
            status = f"waived until {waived}"
        else:
            status = "BLOCKING: fix it or add a waiver"
            blocking += 1
        print(f"  {severity.lower():9} {package} {advisory}: {status}")
problems += [f"{blocking} high or critical advisories have no current waiver"] if blocking else []
for p in problems:
    print(f"FAIL: {p}", file=sys.stderr)
if problems:
    raise SystemExit(1)
print("Dependency audit passed.")
endef
export NEARSHOP_AUDIT_PY
