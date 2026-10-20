"""NearShop API entry point."""
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.ai.semantic_index import index
from app.api.router import api_router
from app.core import observability, ratelimit, security_headers
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import AppError

# JSON lines on stdout, one per request from the middleware below (app/core/observability.py), and error
# tracking when NEARSHOP_SENTRY_DSN is set, before the app is created.
observability.configure_logging()
observability.init_error_tracking()
log = logging.getLogger("nearshop")


# The schema version this process found at startup (logged); /api/health/ready checks it again live.
schema_status: dict = {"checked": False}


def check_schema(mongo) -> None:
    """Compare the highest applied migration with the number this code expects, and log an error when
    the database is behind. Read-only: migrations run in the deploy's pre-deploy step (database/migrate.py),
    never in the API process."""
    from database.migrate import check_version

    version = check_version(mongo)
    schema_status.update(checked=True, applied=version.applied, expected=version.expected,
                         behind=version.behind, ahead=version.ahead)
    if version.behind:
        log.error("database schema is behind this code: highest applied migration %d, this release expects %d "
                  "(pending %s). Run python -m database.migrate (on Render, the pre-deploy step).",
                  version.applied, version.expected, version.pending)
    elif version.ahead:
        log.warning("database schema is ahead of this code (migration %d, this release expects %d): "
                    "a rollback; expand-then-contract keeps it compatible.", version.applied, version.expected)
    else:
        log.info("database schema at migration %d, as this release expects", version.applied)


def serves_local_media(environment: str, media_backend: str) -> bool:
    """Whether the API serves photos from its own disk at /media: in development and test. Staging and
    production serve photos from the bucket's public domain (NEARSHOP_MEDIA_BACKEND=s3). Until a deployed
    environment has its bucket, it keeps the local backend and /media (with a startup warning), so its
    photos keep working between deploys as before."""
    return environment in ("development", "test") or media_backend == "local"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.environment != "development" and settings.secret_key == "dev-only-change-me":
        raise RuntimeError("Set NEARSHOP_SECRET_KEY before running outside development")
    if problem := settings.insecure_cookie_problem():
        # A deploy with it false fails here, and the previous version keeps serving (P2.6).
        raise RuntimeError(problem)
    if settings.edge_secret_required and not settings.edge_secret.get_secret_value():
        # Without it every request would get 403: refuse to start instead, so the deploy fails and the
        # previous version keeps serving.
        raise RuntimeError("Set NEARSHOP_EDGE_SECRET (the same value as on Vercel) before running in "
                           f"{settings.environment}, or set NEARSHOP_EDGE_SECRET_REQUIRED=false")
    if settings.rate_limit_mode != "enforce":
        log.info("rate limits: %s (NEARSHOP_RATE_LIMIT_MODE)", settings.rate_limit_mode)
    if settings.media_backend == "local":
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        if settings.environment not in ("development", "test"):
            log.warning("photos are stored on this server's disk (NEARSHOP_MEDIA_BACKEND=local) and every "
                        "redeploy deletes them; set NEARSHOP_MEDIA_BACKEND=s3 and the NEARSHOP_MEDIA_S3_* settings")
    # Background jobs (app/jobs.py): every process ticks every 30 s, but each job runs only in the process
    # holding its lease in job_leases, so it runs once per tick at any instance count. Training never
    # runs here: the retrain cron job does it in its own process.
    scheduler = None
    if settings.enable_scheduler:
        from app.jobs import start_scheduler

        scheduler = start_scheduler()

    # Check the schema version and build the semantic index in the background so startup stays instant.
    def startup_checks():
        try:
            check_schema(Database().mongo)
        except Exception as exc:
            log.error("could not check the database schema version: %s", type(exc).__name__)
        if not settings.warm_on_startup:
            return
        try:
            db = Database()
            index.ensure(db)
            log.info("semantic index ready: %d listings via %s", len(index.product_ids), index.provider_name)
        except Exception as exc:  # the app still serves keyword search
            log.warning("warm-up failed: %s", exc)

    threading.Thread(target=startup_checks, name="startup-checks", daemon=True).start()
    yield
    if scheduler:
        scheduler.shutdown()  # gives up its leases, so another process takes the jobs over at once


app = FastAPI(title="NearShop API", version="1.0.0", lifespan=lifespan,
              description="Hyperlocal inventory discovery: find it nearby, reserve, pick up or get it delivered.")

# Edge-only access, the client's address and rate limits (app/core/ratelimit.py). Added before CORS, so it runs
# inside it: its 403 and 429 answers carry the CORS headers too.
ratelimit.install(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["content-type", "x-nearshop-client", "authorization"],
)
# Security headers on every response, the edge, rate-limit and error answers included, and no-store on
# signed-in and private responses (app/core/security_headers.py, docs/security/headers.md).
security_headers.install(app)
# Wraps the whole middleware stack (CORS and Starlette's own error handling included): every response,
# the 500 of an unhandled exception included, gets its request id and Server-Timing, and every request its
# one log line.
observability.install(app)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "request"
    message = first.get("msg", "Invalid input").removeprefix("Value error, ")
    return JSONResponse(
        {"error": {"code": "validation_error", "message": f"{field}: {message}", "field": field,
                   "details": [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]}},
        status_code=422,
    )


@app.get("/api/health")
def health():
    """Liveness only: the process answers. Monitors use /api/health/ready."""
    # Render sets RENDER_GIT_COMMIT on each deploy; the smoke workflow reads it to know which commit it tests.
    return {"ok": True, "commit": os.environ.get("RENDER_GIT_COMMIT") or None}


SWEEP_JOB = "sweep_expired"
SWEEP_MAX_AGE = timedelta(seconds=90)  # the expiry sweep runs every 30 s (app/jobs.py)


def ping_database(mongo) -> None:
    mongo.command("ping")


def readiness(mongo) -> dict[str, bool]:
    """The readiness checks, as booleans only: the database answers a ping, its schema migrations are
    current (a database ahead of the code, after a rollback, is compatible: expand-then-contract), this
    process's semantic search index is built, and the expiry sweep ran in the last 90 s."""
    from app.jobs import LEASES
    from database.migrate import check_version

    checks = {"database": False, "migrations": False, "search_index": index.built_at > 0, "sweep": False}
    try:
        ping_database(mongo)
        checks["database"] = True
    except Exception:
        return checks
    try:
        checks["migrations"] = not check_version(mongo).behind
    except Exception:
        pass
    try:
        lease = mongo[LEASES].find_one({"_id": SWEEP_JOB}, {"last_run_at": 1}) or {}
        last_run = lease.get("last_run_at")
        checks["sweep"] = last_run is not None and utcnow() - last_run < SWEEP_MAX_AGE
    except Exception:
        pass
    return checks


@app.get("/api/health/ready")
def health_ready():
    """Readiness for monitors: 503 when the database or its migrations fail. A search index still warming
    or a late sweep is degraded with a 200: keyword search and every page still work."""
    checks = readiness(Database().mongo)
    if not (checks["database"] and checks["migrations"]):
        return JSONResponse({"status": "unavailable", "checks": checks}, status_code=503)
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}


@app.get("/api/health/db")
def health_db():
    """Can the API reach MongoDB? Only ok, or the kind of failure: never a name, a count or a message."""
    try:
        ping_database(Database().mongo)
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": type(exc).__name__}, status_code=503)


app.include_router(api_router)
if serves_local_media(settings.environment, settings.media_backend):
    app.mount("/media", StaticFiles(directory=settings.media_dir, check_dir=False), name="media")
