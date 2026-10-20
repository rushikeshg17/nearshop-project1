"""Observability: error tracking, one JSON log line per request, request ids and Server-Timing.

Error tracking (Sentry)
-----------------------
init_error_tracking turns the Sentry SDK on, with its FastAPI integration, only when NEARSHOP_SENTRY_DSN
is set; without it the SDK is never initialised (so it cannot pick up a stray SENTRY_DSN either) and
nothing leaves the process. Events carry the environment (NEARSHOP_ENVIRONMENT) and the release (the git
commit: NEARSHOP_RELEASE, else Render's RENDER_GIT_COMMIT). Before anything is sent, scrub_event drops
cookies, the authorization header, every request header outside a short allow-list (client addresses,
Vercel's location headers and the like), request bodies and the user, and masks anything that looks like
an Indian mobile number, an email address or a street address, anywhere in the event. Traces are sampled
at NEARSHOP_SENTRY_TRACES_SAMPLE_RATE (10%) in general and NEARSHOP_SENTRY_SEARCH_TRACES_SAMPLE_RATE (25%)
for /api/search; health checks are never traced, and a request that continues a browser trace keeps the
browser's decision, so a trace is never cut in half.

Request middleware
------------------
RequestObservabilityMiddleware wraps every HTTP request:
  * the request id is the incoming edge header's value (NEARSHOP_REQUEST_ID_HEADERS, the first one
    present and well-formed: Vercel's x-vercel-id, then x-request-id), or a new random id. It is sent
    back as X-Request-Id, added to every log line written during the request and tagged on Sentry events;
  * exactly one JSON log line per request (logger nearshop.request): method, route template (never the
    raw path), status, duration_ms, db_ms, db_queue_ms, db_calls, request_id and role (from the signed
    session token; never the user id or email);
  * it wraps the whole middleware stack, Starlette's ServerErrorMiddleware included, so a 500 from an
    unhandled exception has the headers as well, and the server's traceback line keeps the request id;
  * a Server-Timing header: app (time in the app until the response starts), db (wall time with database
    work in flight: Collection calls plus waits in the mongo-io gather pool's queue, overlapping work
    counted once) and dbq (the longest wait in that queue).

Database time is measured by instrument_database, which wraps the methods of app.core.database.Collection,
Database.next_ids and Database.gather from here (no change to the database module). Outside a request
the wrappers only read a context variable and call through.
"""
from __future__ import annotations

import contextvars
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from urllib.parse import unquote_plus

from starlette.datastructures import MutableHeaders
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings, settings

log = logging.getLogger("nearshop.request")

# ---------------------------------------------------------------- personal data

# A 10-digit Indian mobile number (6-9 first), with an optional +91, 91 or 0 prefix and an optional space or
# hyphen after the fifth digit, not inside a longer word or number (ids, hashes, timestamps).
_PHONE = re.compile(r"(?<![\w+])(?:\+91[\s-]?|91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\w)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Street addresses: a house number followed by up to four capitalised words and a street word ("12 MG
# Road", "1 Main Road", "#45/2, Gandhi Nagar"), or an ordinal Bengaluru-style "5th Main", "3rd Cross".
_STREET_WORDS = (r"road|rd|street|st|lane|ln|marg|nagar|layout|colony|galli|gali|circle|avenue|ave|"
                 r"extension|extn|bazaar|bazar|market|cross|main|block|sector|stage|phase|halli|pete|pet")
_ADDRESS = re.compile(
    r"(?:#\s?)?\b\d{1,5}[A-Za-z]?(?:[/-]\d{1,5}[A-Za-z]?)?,?\s+(?:[A-Z][\w.'-]*,?\s+){0,4}"
    rf"(?i:{_STREET_WORDS})\b\.?"
    rf"|\b\d{{1,3}}(?:st|nd|rd|th)\s+(?i:main|cross|block|stage|phase|sector)\b"
)
# Keys whose values are identifiers Sentry needs intact; they never hold personal data.
_ID_KEYS = frozenset({"event_id", "trace_id", "span_id", "parent_span_id", "release", "dist", "sentry-trace",
                      "baggage", "request_id"})


# A run of non-space text that may be URL-encoded: it holds a %XX escape or a + (a space in a query string).
_ENCODED = re.compile(r"[^\s]*(?:%[0-9A-Fa-f]{2}|\+)[^\s]*")


def _mask_plain(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return _ADDRESS.sub("[address]", text)


def _mask_encoded(match: re.Match) -> str:
    """A URL or query string (q=tape+98450%2012345, a%40b.com, next=%2Fsearch%3Fq%3D...) decoded, up to
    three times for a URL inside a URL, and masked; unchanged when its decoded form holds nothing to mask."""
    token = decoded = match.group(0)
    for _ in range(3):
        once = unquote_plus(decoded)
        if once == decoded:
            break
        decoded = once
        masked = _mask_plain(decoded)
        if masked != decoded:
            return masked
    return token


def mask_text(text: str) -> str:
    """text with Indian mobile numbers, email addresses and street addresses replaced by placeholders, in
    plain text and in URL-encoded text alike (URLs, query strings, fetch breadcrumbs)."""
    if not text:
        return text
    text = _mask_plain(text)
    if "%" in text or "+" in text:
        text = _ENCODED.sub(_mask_encoded, text)
    return text


def mask_pii(value: Any, key: str | None = None) -> Any:
    """A copy of value (dicts, lists and strings, however nested) with mask_text applied to every string."""
    if isinstance(value, str):
        return value if key in _ID_KEYS else mask_text(value)
    if isinstance(value, dict):
        return {k: mask_pii(v, k if isinstance(k, str) else None) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask_pii(v) for v in value]
    return value


# Request headers an event may keep. Everything else is dropped: cookies, authorization, client addresses
# (x-forwarded-for, x-real-ip), Vercel's x-vercel-ip-* location headers, signatures and future secrets.
SAFE_HEADERS = frozenset({"accept", "accept-encoding", "accept-language", "content-length", "content-type",
                          "host", "origin", "referer", "user-agent", "x-nearshop-client", "x-request-id",
                          "x-vercel-id", "sentry-trace", "baggage"})


def scrub_event(event: dict, hint: dict | None = None) -> dict:
    """Sentry's before_send and before_send_transaction: remove what may identify a person."""
    request = event.get("request")
    if isinstance(request, dict):
        for field in ("cookies", "data", "env"):
            request.pop(field, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: v for k, v in headers.items() if str(k).lower() in SAFE_HEADERS}
        elif headers is not None:
            request.pop("headers")
    event.pop("user", None)
    return mask_pii(event)


def scrub_breadcrumb(crumb: dict, hint: dict | None = None) -> dict:
    return mask_pii(crumb)


# ---------------------------------------------------------------- error tracking


def traces_sampler(context: dict) -> float:
    """10% of requests, 25% of searches, no health checks; a continued browser trace keeps its decision."""
    scope = context.get("asgi_scope") or {}
    path = scope.get("path") or (context.get("transaction_context") or {}).get("name") or ""
    if path.startswith("/api/health"):
        return 0.0
    if context.get("parent_sampled") is not None:
        return 1.0 if context["parent_sampled"] else 0.0
    if path.startswith("/api/search"):
        return settings.sentry_search_traces_sample_rate
    return settings.sentry_traces_sample_rate


def release_name(config: Settings = settings) -> str | None:
    """The git commit this process runs: NEARSHOP_RELEASE, else RENDER_GIT_COMMIT (set by Render)."""
    return config.release.strip() or os.environ.get("RENDER_GIT_COMMIT") or None


def init_error_tracking(config: Settings = settings, *, dsn: str | None = None, transport: Any = None) -> bool:
    """Turn Sentry on when a DSN is configured (or given, for tests). True when it is on."""
    dsn = dsn or config.sentry_dsn.get_secret_value().strip()
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import ignore_logger
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=config.environment,
        release=release_name(config),
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,  # frame variables can hold anything a request carried
        before_send=scrub_event,
        before_send_transaction=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
        traces_sampler=traces_sampler,
        integrations=[StarletteIntegration(transaction_style="url"), FastApiIntegration(transaction_style="url")],
        transport=transport,
    )
    ignore_logger(log.name)  # one line per request is not a breadcrumb worth sending
    return True


# ---------------------------------------------------------------- JSON logs

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("nearshop_request_id", default=None)
# LogRecord's own attributes; any other attribute came from extra= and is written as a field.
_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message, request_id (during a request), any extra=
    fields, and exc (the masked traceback) when an exception is logged. Messages are masked like events."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": mask_text(record.getMessage()),
        }
        if (rid := _request_id.get()) is not None:
            line["request_id"] = rid
        for key, value in vars(record).items():
            if key not in _RESERVED and key not in line and not key.startswith("_"):
                line[key] = mask_pii(value, key)
        if record.exc_info:
            line["exc"] = mask_text(self.formatException(record.exc_info))
        return json.dumps(line, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """JSON lines on stdout for every logger, uvicorn's included. uvicorn's own access log is turned off:
    the request middleware writes the one line per request."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


# ---------------------------------------------------------------- database timing


class RequestTimer:
    """Database time of one request, collected from every thread that works for it."""

    __slots__ = ("_lock", "_spans", "calls", "queue_wait")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[tuple[float, float]] = []
        self.calls = 0
        self.queue_wait = 0.0  # the longest wait in the gather pool's queue, in seconds

    def add_call(self, start: float, end: float) -> None:
        with self._lock:
            self._spans.append((start, end))
            self.calls += 1

    def add_queue_wait(self, submitted: float, started: float) -> None:
        with self._lock:
            self._spans.append((submitted, started))
            self.queue_wait = max(self.queue_wait, started - submitted)

    def db_seconds(self) -> float:
        """Wall time with any database call or queue wait in flight; overlapping spans count once."""
        with self._lock:
            spans = sorted(self._spans)
        total, cur_start, cur_end = 0.0, None, None
        for start, end in spans:
            if cur_end is None or start > cur_end:
                if cur_end is not None:
                    total += cur_end - cur_start
                cur_start, cur_end = start, end
            else:
                cur_end = max(cur_end, end)
        if cur_end is not None:
            total += cur_end - cur_start
        return total


_timer: contextvars.ContextVar[RequestTimer | None] = contextvars.ContextVar("nearshop_request_timer", default=None)
_in_call = threading.local()  # a Collection method calling another (get -> find_one) counts once


def _timed(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        timer = _timer.get()
        if timer is None or getattr(_in_call, "active", False):
            return fn(*args, **kwargs)
        _in_call.active = True
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            _in_call.active = False
            timer.add_call(start, time.perf_counter())

    wrapper.__nearshop_timed__ = True
    return wrapper


def _timed_gather(gather: Callable) -> Callable:
    @wraps(gather)
    def wrapper(self: Any, *fns: Callable[[], Any]) -> list[Any]:
        timer = _timer.get()
        if timer is None:
            return gather(self, *fns)
        submitter = threading.current_thread()
        submitted = time.perf_counter()

        def bind(fn: Callable[[], Any]) -> Callable[[], Any]:
            def run() -> Any:
                if threading.current_thread() is submitter:  # ran inline (a transaction, one query, nested)
                    return fn()
                timer.add_queue_wait(submitted, time.perf_counter())
                token = _timer.set(timer)  # pool threads do not inherit the request's context
                try:
                    return fn()
                finally:
                    _timer.reset(token)

            return run

        return gather(self, *(bind(fn) for fn in fns))

    wrapper.__nearshop_timed__ = True
    return wrapper


def instrument_database() -> None:
    """Time every public Collection method, Database.next_ids and the gather pool's queue. Idempotent."""
    from app.core.database import Collection, Database

    for name, attr in list(vars(Collection).items()):
        if not name.startswith("_") and inspect.isfunction(attr) and not getattr(attr, "__nearshop_timed__", False):
            setattr(Collection, name, _timed(attr))
    if not getattr(Database.next_ids, "__nearshop_timed__", False):
        Database.next_ids = _timed(Database.next_ids)
    if not getattr(Database.gather, "__nearshop_timed__", False):
        Database.gather = _timed_gather(Database.gather)


# ---------------------------------------------------------------- the middleware

_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def request_id_from(headers: Iterable[tuple[bytes, bytes]], names: Iterable[str]) -> str:
    """The first well-formed id among the named headers, else a new random one."""
    found = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
    for name in names:
        value = found.get(name, "").strip()
        if _REQUEST_ID.fullmatch(value):
            return value
    return uuid.uuid4().hex


def role_from(headers: Iterable[tuple[bytes, bytes]]) -> str:
    """The role in the request's signed session token (cookie or bearer), or anonymous. Never a user id."""
    from app.core.security import decode_access_token

    cookie = bearer = None
    for key, value in headers:
        name = key.lower()
        if name == b"cookie" and cookie is None:
            cookie = cookie_parser(value.decode("latin-1")).get(settings.cookie_name)
        elif name == b"authorization" and value[:7].lower() == b"bearer ":
            bearer = value[7:].decode("latin-1")
    token = cookie or bearer  # as app.core.deps.get_optional_user: the cookie first
    payload = decode_access_token(token) if token else None
    role = payload.get("role") if payload else None
    return role if isinstance(role, str) and role.isidentifier() else "anonymous"


def route_template(scope: Scope) -> str:
    """The matched route's path template, with its router prefixes (/api/products/{product_id})."""
    # FastAPI 0.137 and later keep the prefix-resolved path on the effective route context, while
    # scope["route"].path is the path inside its router (/products/{product_id}).
    context = (scope.get("fastapi") or {}).get("effective_route_context")
    route = scope.get("route")
    for candidate in (context, route):
        path = getattr(candidate, "path_format", None) or getattr(candidate, "path", None)
        if path:
            return path
    if scope.get("app_root_path") or scope.get("root_path"):
        return f"{scope.get('root_path', '')}/{{path}}"  # a mounted app (/media): never the file name
    return "unmatched"


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


class RequestObservabilityMiddleware:
    """Request id, one JSON log line and a Server-Timing header per HTTP request (see the module docstring)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.id_headers = [h.strip().lower() for h in settings.request_id_headers.split(",") if h.strip()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        headers = scope.get("headers") or []
        request_id = request_id_from(headers, self.id_headers)
        role = role_from(headers)
        timer = RequestTimer()
        timer_token, id_token = _timer.set(timer), _request_id.set(request_id)
        _tag_sentry(request_id, role)
        status = 500

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                out = MutableHeaders(scope=message)
                out["X-Request-Id"] = request_id
                out.append("Server-Timing", f"app;dur={_ms(time.perf_counter() - started)}, "
                                            f"db;dur={_ms(timer.db_seconds())}, dbq;dur={_ms(timer.queue_wait)}")
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            fields = {
                "method": scope.get("method"), "route": route_template(scope), "status": status,
                "duration_ms": _ms(time.perf_counter() - started), "db_ms": _ms(timer.db_seconds()),
                "db_queue_ms": _ms(timer.queue_wait), "db_calls": timer.calls, "role": role,
            }
            log.log(logging.WARNING if status >= 500 else logging.INFO, "request", extra=fields)
            _timer.reset(timer_token)
        # Reached only when the app returned. An unhandled exception leaves the request id set: the server
        # (uvicorn) logs the traceback next, in this request's own task, so that line carries the id too.
        # The task ends with the request, so the id goes nowhere else.
        _request_id.reset(id_token)


def _tag_sentry(request_id: str, role: str) -> None:
    if "sentry_sdk" not in sys.modules:
        return
    import sentry_sdk

    if sentry_sdk.get_client().is_active():
        sentry_sdk.set_tag("request_id", request_id)
        sentry_sdk.set_tag("role", role)


def install(app: Any) -> None:
    """Wrap the app's whole middleware stack in the request middleware, and add the database timing.

    The middleware sits outside Starlette's ServerErrorMiddleware too (add_middleware would put it inside),
    so the 500 an unhandled exception turns into also carries X-Request-Id and Server-Timing, and its log
    line its real status. Sentry's ASGI wrapper (Starlette.__call__) stays outside it, as before."""
    instrument_database()
    build_stack = app.build_middleware_stack

    def build_observed_stack() -> ASGIApp:
        return RequestObservabilityMiddleware(build_stack())

    app.build_middleware_stack = build_observed_stack
