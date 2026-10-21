"""Edge-only access, the visitor's address and rate limits (P2.3; plan section 8.3).

Edge-only access
----------------
The browser reaches the API only through Vercel: frontend/src/proxy.ts removes any X-NearShop-Edge and
X-NearShop-Client-IP headers a visitor sent, then adds X-NearShop-Edge carrying NEARSHOP_EDGE_SECRET and
X-NearShop-Client-IP carrying the visitor's address as Vercel gives it to the proxy. Server-rendered pages
(P2.8) call the API from the Next.js server with the same two headers. Where NEARSHOP_EDGE_SECRET_REQUIRED
holds (by default in staging and production), every request without the secret, compared in constant time,
gets 403, except the health checks in EXEMPT_PATHS (Render's own check and the direct uptime monitor). Both
headers are removed from the request before the app sees it.

The client's address
--------------------
X-NearShop-Client-IP is believed only when the request carries the valid edge secret; otherwise (development,
or a header sent without the secret) the client is the TCP peer. Routes read it through
app.core.deps.ClientIP, and every request log line (logger nearshop.request) carries it as client_ip.
Per-IP limits count an IPv6 address by its /64 network: one mobile device is given a whole /64.

Rate limits
-----------
LIMITS is the one table: each row names a method and a route template (exactly as the routes declare it,
/api/products/{product_id}), or every route of one router, and the rules that apply. Rows for single routes
are checked before rows for whole routers, and the first matching row decides, so routes need no edits. A
rule counts requests per key in fixed windows; its key is the client IP, the signed-in user, the session
(the session token) or a field of the JSON request body (body:email). A request is not counted by a rule
whose key it lacks (an anonymous visitor has no user), except that a body key fails closed: when the field
cannot be read (a body over MAX_BODY_BYTES or cut short, not a JSON object, the field missing or not a
string), the request counts under one fallback key shared by every such request, so padding or mangling the
body never escapes a per-account limit. A body field is read as the route reads it (BODY_NORMALISERS: an
email as pydantic's EmailStr normalises it, so a variant spelling of the same address is the same key).
Every request a rule counted by IP does not also counts against DEFAULT, 600 per IP per minute, so no route
is unlimited, the table's routes included.

  * Over a limit the answer is 429 with code rate_limited and Retry-After in seconds, until the window ends.
    A refused request is taken back from every counter, so it uses up none of its other allowances (a
    refused hourly attempt does not count towards the daily limit).
  * failures_only rules (password login) count only answers in FAILURE_STATUSES. Every attempt is counted
    before it is served, with the same atomic $inc, so concurrent attempts cannot all pass the check; one
    past the limit is refused, and the count is taken back when the answer is not a failure. A rule with a
    cooldown, once a failure reaches the limit, blocks its key for the cooldown and starts counting afresh
    after it.
  * admin_exempt rules (search and suggest) do not count signed-in admins; DEFAULT still does.

Counters live in the rate_limits collection, shared by every instance: an atomic upsert and $inc per rule,
on _id "<rule>:<window start>:<hash>", with a TTL index on expires_at (database/schema.py, migration 3). The
key is stored only as an HMAC (keyed with NEARSHOP_SECRET_KEY), never as an address, id or email. If the
database cannot be reached the request is served (fail open) and a warning is logged.

NEARSHOP_RATE_LIMIT_MODE: enforce, log_only or off (config.py has the defaults). In log_only nothing is
refused; a request that would have been gets rate_limited (the rules' names) and rate_limit_mode on its
request log line, so the share of requests that would have been limited is the count of request lines with
rate_limited divided by the count of request lines.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import importlib
import ipaddress
import json
import logging
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import anyio.to_thread
from pydantic import EmailStr, TypeAdapter, ValidationError
from pymongo import ReturnDocument
from pymongo.collection import Collection as MongoCollection
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse
from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

log = logging.getLogger("nearshop.ratelimit")

EDGE_HEADER = "x-nearshop-edge"
CLIENT_IP_HEADER = "x-nearshop-client-ip"
_STRIPPED = frozenset({EDGE_HEADER.encode(), CLIENT_IP_HEADER.encode()})
# Liveness and readiness: Render's health check and the direct uptime monitor reach the API without Vercel, and
# a liveness check must not wait on the database. Neither edge-checked nor counted.
EXEMPT_PATHS = frozenset({"/api/health", "/api/health/ready"})
COLLECTION = "rate_limits"
FAILURE_STATUSES = frozenset({401})  # a failed password login (invalid_credentials)
MAX_BODY_BYTES = 16 * 1024  # bodies read for a body: key; a larger one counts under UNREADABLE
# The body: key part of a request whose field cannot be read: one counter shared by all of them (fail closed).
UNREADABLE = "\x00unreadable"

MINUTE, HOUR, DAY = 60, 60 * 60, 24 * 60 * 60

# Key parts.
IP, USER, SESSION = "ip", "user", "session"


def body(field: str) -> str:
    """A key part taken from a field of the JSON request body, compared trimmed and lower-cased (and as
    BODY_NORMALISERS has it for that field)."""
    return f"body:{field}"


@lru_cache(maxsize=1)
def _email_adapter() -> TypeAdapter[str]:
    return TypeAdapter(EmailStr)


def _email(value: str) -> str:
    """An email as the routes' EmailStr reads it (IDNA-normalised domain, NFC), or as given if invalid."""
    try:
        return _email_adapter().validate_python(value).lower()
    except ValidationError:
        return value


# How a body field becomes its key, after trimming and lower-casing: the form the route itself looks up.
BODY_NORMALISERS: dict[str, Callable[[str], str]] = {"email": _email}


@dataclass(frozen=True)
class Rule:
    name: str  # also the counter's name: rows naming the same rule share its counters
    limit: int
    per: int  # window length in seconds
    key: tuple[str, ...]
    failures_only: bool = False
    cooldown: int = 0  # seconds the key is blocked once the limit is reached (failures_only rules)
    admin_exempt: bool = False


@dataclass(frozen=True)
class Row:
    methods: tuple[str, ...]
    rules: tuple[Rule, ...]
    route: str = ""  # a route template, as the route declares it with the /api prefix
    router: str = ""  # or the module of a router: every route of it, with the methods above


# ---------------------------------------------------------------- the table (plan section 8.3)

LOGIN_ACCOUNT_IP = Rule("login_account_ip", 5, 15 * MINUTE, (body("email"), IP), failures_only=True)
LOGIN_IP = Rule("login_ip", 20, 15 * MINUTE, (IP,), failures_only=True)
LOGIN_ACCOUNT = Rule("login_account", 10, HOUR, (body("email"),), failures_only=True, cooldown=30 * MINUTE)
# Password change (P2.4; no row in plan section 8.3, so the per-account login numbers are followed). Every attempt
# counts, not only failures: a wrong current password answers 400, which FAILURE_STATUSES does not count, and a
# held session cookie must not allow guessing the current password at the default rate.
PASSWORD_CHANGE_HOUR = Rule("password_change_hour", 5, HOUR, (USER,))
PASSWORD_CHANGE_DAY = Rule("password_change_day", 10, DAY, (USER,))
REGISTER_HOUR = Rule("register_hour", 3, HOUR, (IP,))
REGISTER_DAY = Rule("register_day", 10, DAY, (IP,))
RESERVATION_HOUR = Rule("reservation_hour", 10, HOUR, (USER,))
RESERVATION_DAY = Rule("reservation_day", 30, DAY, (USER,))
ORDER_HOUR = Rule("order_hour", 5, HOUR, (USER,))
ORDER_DAY = Rule("order_day", 15, DAY, (USER,))
REVIEW_DAY = Rule("review_day", 5, DAY, (USER,))
SEARCH_IP = Rule("search_ip", 120, MINUTE, (IP,), admin_exempt=True)
SEARCH_SESSION = Rule("search_session", 30, MINUTE, (SESSION,), admin_exempt=True)
SUGGEST_IP = Rule("suggest_ip", 240, MINUTE, (IP,), admin_exempt=True)
SEARCH_COMPARE_IP = Rule("search_compare_ip", 10, MINUTE, (IP,))
IMAGE_UPLOAD = Rule("image_upload", 30, HOUR, (USER,))
IMPORT_PREVIEW = Rule("import_preview", 20, HOUR, (USER,))
IMPORT_COMMIT = Rule("import_commit", 10, HOUR, (USER,))
DATA_EXPORT = Rule("data_export", 3, DAY, (USER,))
CATALOG_READ = Rule("catalog_read", 300, MINUTE, (IP,))
DEFAULT = Rule("default", 600, MINUTE, (IP,))

# Later prompts add their rows here, with the plan section 8.3 limits: review report (P3.6), grievance
# submission keyed by body("contact") (P3.8), OTP send and verify keyed by body("phone") (P4.1), search result
# click (P5.6) and lead event (P9.2).
LIMITS: tuple[Row, ...] = (
    Row(("POST",), (LOGIN_ACCOUNT_IP, LOGIN_IP, LOGIN_ACCOUNT), route="/api/auth/login"),
    Row(("POST",), (PASSWORD_CHANGE_HOUR, PASSWORD_CHANGE_DAY), route="/api/auth/password"),
    Row(("POST",), (REGISTER_HOUR, REGISTER_DAY), route="/api/auth/register"),
    Row(("POST",), (REGISTER_HOUR, REGISTER_DAY), route="/api/auth/register-shop"),
    Row(("POST",), (RESERVATION_HOUR, RESERVATION_DAY), route="/api/reservations"),
    Row(("POST",), (ORDER_HOUR, ORDER_DAY), route="/api/orders"),
    Row(("POST",), (REVIEW_DAY,), route="/api/reviews"),
    Row(("GET",), (SEARCH_IP, SEARCH_SESSION), route="/api/search"),
    Row(("GET",), (SUGGEST_IP,), route="/api/search/suggest"),
    Row(("GET",), (SEARCH_COMPARE_IP,), route="/api/ai/search-compare"),
    Row(("POST",), (IMAGE_UPLOAD,), route="/api/owner/shop/image"),
    Row(("POST",), (IMAGE_UPLOAD,), route="/api/owner/products/{product_id}/image"),
    Row(("POST",), (IMPORT_PREVIEW,), route="/api/owner/import/preview"),
    Row(("POST",), (IMPORT_COMMIT,), route="/api/owner/import/commit"),
    Row(("GET",), (DATA_EXPORT,), route="/api/me/export"),  # the data export (P3.2)
    Row(("GET",), (CATALOG_READ,), route="/api/meta"),
    # Product, recommendations, shop list, shop, shop products and reviews, and P2.8's sitemap endpoint.
    Row(("GET",), (CATALOG_READ,), router="app.api.routes.catalog"),
)
# Routes in LIMITS that a later prompt creates (the table test allows them to be missing until then).
PLANNED_ROUTES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CompiledRow:
    method: str
    template: str
    pattern: re.Pattern[str]
    rules: tuple[Rule, ...]


@lru_cache(maxsize=1)
def compiled_rows() -> tuple[CompiledRow, ...]:
    """LIMITS with router rows expanded to their routes' templates: single routes first, then routers."""
    from app.api.router import api_router  # here, not at import: the routes import app.core.deps

    single: list[tuple[str, str, tuple[Rule, ...]]] = []
    routers: list[tuple[str, str, tuple[Rule, ...]]] = []
    for row in LIMITS:
        if row.router:
            for route in importlib.import_module(row.router).router.routes:
                methods = getattr(route, "methods", None) or set()
                routers += [(m, api_router.prefix + route.path, row.rules) for m in row.methods if m in methods]
        else:
            single += [(m, row.route, row.rules) for m in row.methods]
    return tuple(CompiledRow(m, template, compile_path(template)[0], rules) for m, template, rules in single + routers)


def match(method: str, path: str) -> CompiledRow | None:
    """The table row for a request, or None (then only DEFAULT applies). HEAD is counted as GET."""
    method = "GET" if method == "HEAD" else method
    for row in compiled_rows():
        if row.method == method and row.pattern.match(path):
            return row
    return None


# ---------------------------------------------------------------- who is asking


def edge_secret_valid(headers: Headers) -> bool:
    """Whether the request carries NEARSHOP_EDGE_SECRET in X-NearShop-Edge (constant-time comparison)."""
    secret = settings.edge_secret.get_secret_value()
    given = headers.get(EDGE_HEADER)
    if not secret or given is None:
        return False
    # Starlette decodes header bytes as latin-1, so this gives back exactly the bytes that were sent.
    return hmac.compare_digest(given.encode("latin-1"), secret.encode("utf-8"))


def parse_ip(value: str) -> str | None:
    """value as a normalised IP address (an IPv4-mapped IPv6 address as IPv4), or None if it is not one."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def client_address(scope: Scope, headers: Headers, trusted: bool) -> str:
    """X-NearShop-Client-IP when the edge secret is valid and the header holds an address, else the TCP peer."""
    if trusted and (ip := parse_ip(headers.get(CLIENT_IP_HEADER, ""))):
        return ip
    client = scope.get("client")
    return (parse_ip(client[0]) or client[0]) if client else "unknown"


def ip_key(ip: str) -> str:
    """What per-IP limits count: the address, or the /64 network of an IPv6 address."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return ip


@dataclass(frozen=True)
class Identity:
    user: str | None = None  # the user id in a valid session token
    session: str | None = None  # a hash of that token
    admin: bool = False


def identity_from(headers: Headers) -> Identity:
    """The signed-in user from the session cookie, else a bearer token, as app.core.deps.get_optional_user
    reads it (the signature and expiry only; a deactivated account still counts as that user)."""
    from app.core.security import decode_access_token

    token = cookie_parser(headers.get("cookie", "")).get(settings.cookie_name)
    if not token:
        auth = headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
    payload = decode_access_token(token) if token else None
    if not payload or not payload.get("sub"):
        return Identity()
    return Identity(user=str(payload["sub"]), session=hashlib.sha256(token.encode()).hexdigest()[:32],
                    admin=payload.get("role") == "admin")


def key_digest(parts: Sequence[str]) -> str:
    """The stored form of a key: an HMAC, so the collection never holds an address, an id or an email."""
    return hmac.new(settings.secret_key.encode(), "\x1f".join(parts).encode(), hashlib.sha256).hexdigest()[:32]


# ---------------------------------------------------------------- counters

now: Callable[[], float] = time.time  # tests replace it to move between windows


def _as_date(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)  # naive UTC, like app.core.database.utcnow


def _window(rule: Rule, ts: float) -> tuple[int, int]:
    start = int(ts // rule.per) * rule.per
    return start, start + rule.per


def _retry_after(until: float, ts: float) -> int:
    return max(1, math.ceil(until - ts))


@dataclass(frozen=True)
class Hit:
    rule: Rule
    digest: str


class Counters:
    """The rate_limits collection. Synchronous (PyMongo): the middleware calls it in a worker thread."""

    def __init__(self, collection: MongoCollection) -> None:
        self.collection = collection

    def _incr(self, hit: Hit, ts: float) -> tuple[int, float]:
        start, end = _window(hit.rule, ts)
        update = {"$inc": {"count": 1}, "$setOnInsert": {"rule": hit.rule.name, "expires_at": _as_date(end)}}
        for attempt in (1, 2):
            try:
                doc = self.collection.find_one_and_update({"_id": f"{hit.rule.name}:{start}:{hit.digest}"}, update,
                                                          upsert=True, return_document=ReturnDocument.AFTER)
                return doc["count"], end
            except DuplicateKeyError:  # two first hits raced on the upsert; the second one updates
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def undo(self, hits: Sequence[Hit], ts: float) -> None:
        """Take back one count from each hit, in the window of ts (when the request was counted)."""
        for hit in hits:
            start, _ = _window(hit.rule, ts)
            self.collection.update_one({"_id": f"{hit.rule.name}:{start}:{hit.digest}", "count": {"$gt": 0}},
                                       {"$inc": {"count": -1}})

    def check(self, counted: Sequence[Hit], failures: Sequence[Hit], ts: float, *, refuse: bool
              ) -> tuple[list[tuple[Rule, int]], list[int]]:
        """Count the request against every hit (against failures_only ones as an attempt, which settle()
        keeps or takes back once the answer is known) and return the rules it is over, with Retry-After, and
        the failures' counts. Over means a counter past its limit or a cooldown running. The cooldowns are read
        after counting, so an attempt racing the failure that starts one either sees it or was counted before
        it. When refuse is set and the request is over any rule, every count is taken back."""
        hits = [*counted, *failures]
        over: dict[str, tuple[Rule, int]] = {}

        def add(rule: Rule, retry: int) -> None:
            if retry > over.get(rule.name, (rule, 0))[1]:
                over[rule.name] = (rule, retry)

        counts = []
        for hit in hits:
            count, end = self._incr(hit, ts)
            counts.append(count)
            if count > hit.rule.limit:
                add(hit.rule, _retry_after(end, ts))
        for rule, until in self._cooldowns(failures, ts):
            add(rule, _retry_after(until, ts))
        if over and refuse:
            self.undo(hits, ts)
        return list(over.values()), counts[len(counted):]

    def _cooldowns(self, hits: Sequence[Hit], ts: float) -> list[tuple[Rule, float]]:
        """The cooldowns running for these hits, with their end."""
        ids = {f"{hit.rule.name}:cooldown:{hit.digest}": hit.rule for hit in hits if hit.rule.cooldown}
        if not ids:
            return []
        running = []
        for doc in self.collection.find({"_id": {"$in": list(ids)}}):
            # Until expires_at: the TTL monitor deletes it up to a minute later.
            until = doc["expires_at"].replace(tzinfo=UTC).timestamp()
            if until > ts:
                running.append((ids[doc["_id"]], until))
        return running

    def settle(self, hits: Sequence[Hit], counts: Sequence[int], ts: float, *, failed: bool, at: float) -> None:
        """Once the answer is known: a failure keeps its counts, and one that reached a rule's limit starts its
        cooldown (from at) and its counting afresh; any other answer takes the counts back (from the window of
        ts, when they were made)."""
        if not failed:
            self.undo(hits, ts)
            return
        for hit, count in zip(hits, counts, strict=True):
            if hit.rule.cooldown and count >= hit.rule.limit:
                start, _ = _window(hit.rule, ts)
                # The cooldown first, then the counter: an attempt counted in between still sees the cooldown.
                self.collection.update_one(
                    {"_id": f"{hit.rule.name}:cooldown:{hit.digest}"},
                    {"$max": {"expires_at": _as_date(at + hit.rule.cooldown)},
                     "$setOnInsert": {"rule": hit.rule.name, "count": 0}}, upsert=True)
                self.collection.delete_one({"_id": f"{hit.rule.name}:{start}:{hit.digest}"})


def counters() -> Counters:
    from app.core.database import Database

    return Counters(Database().mongo[COLLECTION])


# ---------------------------------------------------------------- the middleware

# What the request log line adds (client_ip, and rate_limited with rate_limit_mode), set for each request by
# the middleware; RequestFields copies it onto the nearshop.request record.
_request_fields: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nearshop_request_fields", default=None)


class RequestFields(logging.Filter):
    """Adds the current request's client_ip (and rate-limit outcome) to records of the logger it is on."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_request_fields.get() or {}).items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def forbidden() -> JSONResponse:
    return _error(403, "forbidden", "Forbidden")


def too_many_requests(retry_after: int) -> JSONResponse:
    return _error(429, "rate_limited", "Too many requests. Please wait a little and try again.",
                  {"Retry-After": str(retry_after)})


async def _buffer_body(receive: Receive) -> tuple[bytes | None, Receive]:
    """The request body (None when it is larger than MAX_BODY_BYTES or the client left), and a receive that
    gives the app every message again."""
    messages: list[Message] = []
    data, complete = b"", False
    while len(data) <= MAX_BODY_BYTES:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        data += message.get("body", b"")
        if not message.get("more_body", False):
            complete = len(data) <= MAX_BODY_BYTES
            break

    async def replay() -> Message:
        return messages.pop(0) if messages else await receive()

    return (data if complete else None), replay


def _body_fields(data: bytes | None) -> dict[str, str]:
    try:
        parsed = json.loads(data) if data else None
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    fields = {}
    for key, value in parsed.items():
        if isinstance(value, str) and (value := value.strip().lower()):
            fields[key] = BODY_NORMALISERS.get(key, str)(value)
    return fields


class EdgeRateLimitMiddleware:
    """The edge check, the client's address and the rate limits, for every HTTP request (module docstring)."""

    def __init__(self, app: ASGIApp, store: Callable[[], Counters] = counters) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        trusted = edge_secret_valid(headers)
        ip = client_address(scope, headers, trusted)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() not in _STRIPPED]
        scope.setdefault("state", {})["client_ip"] = ip
        fields: dict[str, Any] = {"client_ip": ip}
        _request_fields.set(fields)  # not reset: the request log line is written after this returns
        path = scope.get("path", "")
        exempt = path in EXEMPT_PATHS
        if settings.edge_secret_required and not trusted and not exempt:
            await forbidden()(scope, receive, send)
            return
        mode = settings.rate_limit_mode
        if exempt or mode == "off":
            await self.app(scope, receive, send)
            return

        row = match(scope.get("method", "GET"), path)
        rules = row.rules if row else ()
        identity = identity_from(headers)
        body_values: dict[str, str] = {}
        if any(part.startswith("body:") for rule in rules for part in rule.key):
            data, receive = await _buffer_body(receive)
            body_values = _body_fields(data)
        hits = []
        for rule in rules:
            if rule.admin_exempt and identity.admin:
                continue
            parts = [self._key_part(part, ip, identity, body_values) for part in rule.key]
            if all(parts):  # a body: part is never empty (UNREADABLE), only a missing user or session is
                hits.append(Hit(rule, key_digest([rule.name, *parts])))
        if not any(IP in h.rule.key and not h.rule.failures_only for h in hits):
            hits.append(Hit(DEFAULT, key_digest([DEFAULT.name, ip_key(ip)])))
        counted = [h for h in hits if not h.rule.failures_only]
        failures = [h for h in hits if h.rule.failures_only]

        ts = now()
        try:
            store = self.store()
            over, failure_counts = await anyio.to_thread.run_sync(
                lambda: store.check(counted, failures, ts, refuse=mode == "enforce"))
        except PyMongoError as exc:
            log.warning("rate limits not checked, request served: %s", type(exc).__name__)
            store, over, failure_counts = None, [], []
        if over:
            fields.update(rate_limited=",".join(rule.name for rule, _ in over), rate_limit_mode=mode)
            if mode == "enforce":
                await too_many_requests(max(retry for _, retry in over))(scope, receive, send)
                return

        if not failures or store is None:
            await self.app(scope, receive, send)
            return

        # The attempt is already counted; settle it once, when the answer starts (before it leaves, so the
        # client's next attempt sees the cooldown it may start), or when the app ends without one.
        settled = False

        async def settle(failed: bool) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            try:
                await anyio.to_thread.run_sync(
                    lambda: store.settle(failures, failure_counts, ts, failed=failed, at=now()))
            except PyMongoError as exc:
                log.warning("login attempt not settled: %s", type(exc).__name__)

        async def send_settling(message: Message) -> None:
            if message["type"] == "http.response.start":
                await settle(message["status"] in FAILURE_STATUSES)
            await send(message)

        try:
            await self.app(scope, receive, send_settling)
        finally:
            with anyio.CancelScope(shield=True):  # also when the client left and the request was cancelled
                await settle(False)

    @staticmethod
    def _key_part(part: str, ip: str, identity: Identity, body_values: dict[str, str]) -> str | None:
        if part == IP:
            return ip_key(ip)
        if part == USER:
            return identity.user
        if part == SESSION:
            return identity.session
        if part.startswith("body:"):
            return body_values.get(part.removeprefix("body:")) or UNREADABLE  # fail closed
        raise ValueError(f"unknown rate-limit key part {part!r}")


_FILTER = RequestFields()


def install(app: Any) -> None:
    """Add the middleware (inside CORS, which main.py adds after it) and client_ip on request log lines."""
    app.add_middleware(EdgeRateLimitMiddleware)
    request_log = logging.getLogger("nearshop.request")
    if _FILTER not in request_log.filters:
        request_log.addFilter(_FILTER)
