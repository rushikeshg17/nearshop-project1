"""NearShop API entry point."""
import logging
import os
import re
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.ai.semantic_index import index
from app.api.router import api_router
from app.core.config import settings
from app.core.database import Database
from app.core.errors import AppError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nearshop")
logging.getLogger("apscheduler").setLevel(logging.WARNING)


# The schema version this process found at startup. P2.1 turns "behind" into a readiness failure.
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
    if settings.media_backend == "local":
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        if settings.environment not in ("development", "test"):
            log.warning("photos are stored on this server's disk (NEARSHOP_MEDIA_BACKEND=local) and every "
                        "redeploy deletes them; set NEARSHOP_MEDIA_BACKEND=s3 and the NEARSHOP_MEDIA_S3_* settings")
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
        scheduler.shutdown(wait=False)


app = FastAPI(title="NearShop API", version="1.0.0", lifespan=lifespan,
              description="Hyperlocal inventory discovery: find it nearby, reserve, pick up or get it delivered.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["content-type", "x-nearshop-client", "authorization"],
)


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
    # Render sets RENDER_GIT_COMMIT on each deploy; the smoke workflow reads it to know which commit it tests.
    return {"ok": True, "commit": os.environ.get("RENDER_GIT_COMMIT") or None}


@app.get("/api/health/db")
def health_db():
    """Can the API reach MongoDB? Reports the kind of failure, never the connection string."""
    try:
        db = Database()
        db.mongo.command("ping")
        return {"ok": True, "database": db.mongo.name, "shops": db.shops.count()}
    except Exception as exc:
        detail = re.sub(r"//[^@/\s]*@", "//***@", str(exc))[:300]  # never echo credentials
        return JSONResponse({"ok": False, "error": type(exc).__name__, "detail": detail}, status_code=503)


app.include_router(api_router)
if serves_local_media(settings.environment, settings.media_backend):
    app.mount("/media", StaticFiles(directory=settings.media_dir, check_dir=False), name="media")
