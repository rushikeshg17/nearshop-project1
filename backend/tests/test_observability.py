"""Observability (app/core/observability.py): error events scrubbed before sending, one JSON log line per
request, request ids, Server-Timing, and the health endpoints (/api/health/ready, /api/health/db)."""
import asyncio
import io
import json
import logging
import re
import threading
import time

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app import main
from app.ai.semantic_index import index
from app.core import observability
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.observability import RequestTimer, mask_text, scrub_event, traces_sampler
from app.jobs import LEASES
from database import migrate
from tests.conftest import CENTER, PASSWORD

PHONE = "98450 12345"
EMAIL = "someone.real@example.com"
STREET = "12 MG Road"
FAKE_DSN = "https://public0123456789abcdef@o0.ingest.example.invalid/1"


# ------------------------------------------------------------------ masking


@pytest.mark.parametrize("text", [
    "call 9845012345 now", "call +91 98450 12345 now", "call +919845012345", "call 09845012345",
    "call 98450-12345", "phone=9845012345&x=1",
    # URL-encoded: a + or %20 for a space, %2B for the plus, and a URL inside a URL
    "q=tape+9845012345", "q=tape%209845012345", "/api/search?q=call+%2B919845012345", "q=98450%2012345",
    "https://x.test/login?next=%2Fsearch%3Fq%3Dtape%2B9845012345",
])
def test_mobile_numbers_are_masked(text):
    masked = mask_text(text)
    assert "[phone]" in masked and not re.search(r"\d{5}", masked)


@pytest.mark.parametrize("text,gone", [
    (f"mail {EMAIL} today", EMAIL),
    ("mongodb+srv://user:secret@cluster0.abcd1.mongodb.net/x", "secret@cluster0"),
    ("lives at 12 MG Road, Ballari", "MG Road"),
    ("1 Main Road", "Main Road"),
    ("#45/2, Gandhi Nagar", "Gandhi Nagar"),
    ("off 5th Main, 3rd Cross", "5th Main"),
    ("q=someone.real%40example.com", "example.com"),
    ("/api/search?q=12+MG+Road", "MG"),
    ("q=12%20MG%20Road&lat=1", "MG"),
])
def test_emails_and_street_addresses_are_masked(text, gone):
    assert gone not in mask_text(text)


@pytest.mark.parametrize("text", [
    'File "/app/main.py", line 12, in main',  # tracebacks stay readable
    "order 512 is ready", "listing 1234567 updated", "took 1792487642 ms",
    "trace 9845012345abcdef9845012345abcdef",  # hex ids are never phone numbers
    "/api/search?q=teflon+tape&lat=12.3052&lng=76.6552", "C++ is 50% off", "q=bad%zz+value",
])
def test_ordinary_text_is_left_alone(text):
    assert mask_text(text) == text


def test_scrub_event_drops_cookies_auth_body_user_and_client_addresses():
    event = {
        "request": {"url": f"https://x.test/api/search?q={PHONE}", "cookies": {"nearshop_session": "tok"},
                    "data": {"phone": PHONE}, "env": {"REMOTE_ADDR": "10.0.0.1"},
                    "headers": {"Cookie": "nearshop_session=tok", "Authorization": "Bearer tok",
                                "X-Forwarded-For": "1.2.3.4", "x-vercel-ip-city": "Ballari",
                                "User-Agent": "test-agent", "x-request-id": "abc"}},
        "user": {"id": 7, "email": EMAIL},
        "exception": {"values": [{"type": "RuntimeError", "value": f"failed for {EMAIL} at {STREET}"}]},
        "contexts": {"trace": {"trace_id": "9845012345abcdef9845012345abcdef"}},
    }
    out = scrub_event(event)
    text = json.dumps(out)
    for secret in ("tok", PHONE, EMAIL, "MG Road", "1.2.3.4", "10.0.0.1", "Ballari", '"user"', "cookies"):
        assert secret not in text
    assert out["request"]["headers"] == {"User-Agent": "test-agent", "x-request-id": "abc"}
    assert out["contexts"]["trace"]["trace_id"] == "9845012345abcdef9845012345abcdef"


# ------------------------------------------------------------------ error tracking


class FakeTransport(Transport):
    """Keeps every envelope instead of sending it."""

    def __init__(self):
        super().__init__({"dsn": FAKE_DSN})
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def events(self):
        return [item.payload.json for env in self.envelopes for item in env.items if item.type == "event"]


@pytest.fixture
def sentry():
    transport = FakeTransport()
    assert observability.init_error_tracking(dsn=FAKE_DSN, transport=transport)
    yield transport
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def boom_route():
    """A test route that fails with personal data in its message."""

    def boom():
        raise RuntimeError(f"lookup failed for {PHONE}, {EMAIL}, {STREET}")

    main.app.add_api_route("/api/test-observability-boom", boom, methods=["POST"])
    yield
    main.app.router.routes[:] = [r for r in main.app.router.routes
                                 if getattr(r, "path", None) != "/api/test-observability-boom"]


def test_off_without_a_dsn():
    assert settings.sentry_dsn.get_secret_value() == ""
    assert observability.init_error_tracking() is False


def test_an_error_reaches_the_tracker_without_cookie_or_phone_number(ids, sentry, boom_route):
    with TestClient(main.app, raise_server_exceptions=False) as c:
        c.post("/api/auth/login", json={"email": "cust@example.com", "password": PASSWORD},
               headers={"X-NearShop-Client": "web"})
        session = c.cookies[settings.cookie_name]
        res = c.post("/api/test-observability-boom?q=tape+9845012345&e=someone.real%40example.com",
                     json={"phone": PHONE, "email": EMAIL},
                     headers={"X-NearShop-Client": "web", "Authorization": "Bearer not-a-real-token",
                              "x-vercel-id": "bom1::test-request-1"})
    assert res.status_code == 500
    sentry_sdk.flush()
    events = [e for e in sentry.events() if e.get("exception")]
    assert len(events) == 1
    event = events[0]
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
    assert event["environment"] == "test"
    assert event["tags"]["request_id"] == "bom1::test-request-1" and event["tags"]["role"] == "customer"
    request = event["request"]
    assert "cookies" not in request and "data" not in request
    assert {k.lower() for k in request.get("headers", {})}.isdisjoint({"cookie", "authorization"})
    text = json.dumps(event)  # _meta may say a cookie header was removed, never what it held
    assert session not in text and settings.cookie_name not in text and "not-a-real-token" not in text
    assert request["query_string"] == "q=tape [phone]&e=[email]"
    for personal in (PHONE, PHONE.replace(" ", ""), EMAIL, "MG Road", "cust@example.com", "example.com"):
        assert personal not in text
    assert "[phone]" in text and "[email]" in text


def test_trace_sampling():
    def ctx(path, parent=None):
        return {"asgi_scope": {"path": path}, "parent_sampled": parent, "transaction_context": {}}

    assert traces_sampler(ctx("/api/search")) == 0.25
    assert traces_sampler(ctx("/api/search/suggest")) == 0.25
    assert traces_sampler(ctx("/api/products/7")) == 0.1
    assert traces_sampler(ctx("/api/health/ready")) == 0.0
    assert traces_sampler(ctx("/api/products/7", parent=True)) == 1.0  # the browser's decision
    assert traces_sampler(ctx("/api/search", parent=False)) == 0.0


# ------------------------------------------------------------------ logs, request ids, Server-Timing


@pytest.fixture
def request_log():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(observability.JsonFormatter())
    logger = logging.getLogger("nearshop.request")
    logger.addHandler(handler)
    yield buffer
    logger.removeHandler(handler)


def _timing(res) -> dict[str, float]:
    header = res.headers["server-timing"]
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"(\w+);dur=([\d.]+)", header)}


def test_server_timing_on_responses(client, ids):
    res = client.get(f"/api/products/{ids['charger']}")  # several queries, fanned out through gather
    assert res.status_code == 200
    timing = _timing(res)
    assert set(timing) == {"app", "db", "dbq"}
    assert 0 < timing["db"] <= timing["app"]
    assert client.get("/api/health").headers["server-timing"].startswith("app;dur=")
    assert "server-timing" in client.get("/api/no-such-route").headers


def test_every_request_writes_exactly_one_json_log_line(customer, ids, request_log):
    customer.get("/api/search", params={"q": "teflon tape", "lat": CENTER[0], "lng": CENTER[1]},
                 headers={"x-vercel-id": "bom1::abc-123"})
    customer.get(f"/api/products/{ids['tape']}")
    customer.get("/api/no-such-route")
    lines = request_log.getvalue().splitlines()
    assert len(lines) == 3
    search, product, missing = (json.loads(line) for line in lines)
    assert search["message"] == "request" and search["method"] == "GET" and search["route"] == "/api/search"
    assert search["status"] == 200 and search["role"] == "customer" and search["request_id"] == "bom1::abc-123"
    assert search["duration_ms"] >= search["db_ms"] > 0 and search["db_calls"] > 0
    assert product["route"] == "/api/products/{product_id}"  # the template, never the raw path
    assert missing["route"] == "unmatched" and missing["status"] == 404 and missing["role"] == "customer"
    assert re.fullmatch(r"[0-9a-f]{32}", product["request_id"])  # no edge header: a new id
    for line in lines:
        assert "cust@example.com" not in line and '"user_id"' not in line and "teflon" not in line


def test_request_id_is_echoed_and_malformed_ids_are_replaced(client):
    assert client.get("/api/health", headers={"x-request-id": "edge-42"}).headers["x-request-id"] == "edge-42"
    replaced = client.get("/api/health", headers={"x-request-id": "bad id <script>"}).headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", replaced)


def test_an_unhandled_error_still_writes_one_line(ids, boom_route, request_log):
    with TestClient(main.app, raise_server_exceptions=False) as c:
        res = c.post("/api/test-observability-boom", headers={"X-NearShop-Client": "web", "x-request-id": "edge-500"})
    assert res.status_code == 500
    # the 500 Starlette makes of the exception still says which request it was, and how long it took
    assert res.headers["x-request-id"] == "edge-500" and res.headers["server-timing"].startswith("app;dur=")
    (line,) = request_log.getvalue().splitlines()
    record = json.loads(line)
    assert record["status"] == 500 and record["level"] == "warning" and record["role"] == "anonymous"
    assert record["request_id"] == "edge-500" and record["route"] == "/api/test-observability-boom"


def test_the_servers_traceback_line_keeps_the_request_id(boom_route):
    """uvicorn logs an exception the app raises in the request's own task, after the app returns."""
    sent, buffer = [], io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(observability.JsonFormatter())
    server_log = logging.getLogger("tests.server")
    server_log.addHandler(handler)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/api/test-observability-boom", "raw_path": b"/api/test-observability-boom", "root_path": "",
             "query_string": b"", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"x-nearshop-client", b"web"), (b"x-request-id", b"edge-501")]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def serve():  # as uvicorn's RequestResponseCycle.run_asgi
        try:
            await main.app(scope, receive, send)
        except RuntimeError:
            server_log.exception("Exception in ASGI application")

    try:
        asyncio.run(serve())
    finally:
        server_log.removeHandler(handler)
    (line,) = buffer.getvalue().splitlines()
    record = json.loads(line)
    assert record["request_id"] == "edge-501" and "RuntimeError" in record["exc"]
    assert "[phone]" in record["exc"] and PHONE not in record["exc"]
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 500 and (b"x-request-id", b"edge-501") in start["headers"]


def test_log_messages_are_masked():
    record = logging.makeLogRecord({"name": "x", "levelno": logging.ERROR, "levelname": "ERROR",
                                    "msg": "could not notify %s", "args": (EMAIL,)})
    line = json.loads(observability.JsonFormatter().format(record))
    assert line["message"] == "could not notify [email]" and line["level"] == "error"


def test_database_time_counts_overlapping_work_once():
    timer = RequestTimer()
    timer.add_call(0.0, 0.010)
    timer.add_call(0.005, 0.020)  # overlaps the first (a gathered query in another thread)
    timer.add_queue_wait(0.030, 0.032)
    assert timer.db_seconds() == pytest.approx(0.022)
    assert timer.queue_wait == pytest.approx(0.002) and timer.calls == 2


def test_gather_pool_queue_wait_is_measured(ids):
    timer = RequestTimer()
    token = observability._timer.set(timer)
    try:
        db = Database()
        results = db.gather(*[lambda: (time.sleep(0.01), db.shops.count())[1] for _ in range(30)])
    finally:
        observability._timer.reset(token)
    assert results == [2] * 30
    assert timer.calls == 30  # each count once, from the pool's threads
    assert timer.queue_wait > 0  # 30 tasks on 24 threads: some waited
    assert threading.current_thread().name == "MainThread"


# ------------------------------------------------------------------ health endpoints


@pytest.fixture
def fresh_sweep(db):
    now = utcnow()
    db.mongo[LEASES].update_one({"_id": main.SWEEP_JOB},
                                {"$set": {"holder": "test", "expires_at": now, "last_run_at": now}}, upsert=True)
    yield
    db.mongo[LEASES].delete_one({"_id": main.SWEEP_JOB})


def test_readiness_ok_and_degraded(client, db, fresh_sweep, monkeypatch):
    index.ensure(db)
    res = client.get("/api/health/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "checks": {"database": True, "migrations": True,
                                                       "search_index": True, "sweep": True}}
    monkeypatch.setattr(index, "built_at", 0.0)  # still warming: keyword search works, so 200
    db.mongo[LEASES].update_one({"_id": main.SWEEP_JOB}, {"$set": {"last_run_at": utcnow().replace(year=2020)}})
    res = client.get("/api/health/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "degraded", "checks": {"database": True, "migrations": True,
                                                             "search_index": False, "sweep": False}}


def test_readiness_503_when_the_database_ping_fails(client, monkeypatch):
    def fail(_mongo):
        raise ConnectionError("cluster0.abcd1.mongodb.net unreachable")

    monkeypatch.setattr(main, "ping_database", fail)
    res = client.get("/api/health/ready")
    assert res.status_code == 503
    assert res.json()["status"] == "unavailable" and res.json()["checks"]["database"] is False
    assert all(isinstance(v, bool) for v in res.json()["checks"].values())
    assert "mongodb.net" not in res.text


def test_readiness_503_when_migrations_are_behind(client, monkeypatch):
    monkeypatch.setattr(migrate, "check_version", lambda mongo: migrate.SchemaVersion(applied=1, expected=2,
                                                                                      pending=[2]))
    res = client.get("/api/health/ready")
    assert res.status_code == 503 and res.json()["checks"]["migrations"] is False


def test_health_db_reveals_no_name_or_number(client, db, monkeypatch):
    res = client.get("/api/health/db")
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert db.mongo.name not in res.text and not re.search(r"\d", res.text)

    def fail(_mongo):
        raise ConnectionError(f"{db.mongo.name} at cluster0.abcd1.mongodb.net: 3 shops")

    monkeypatch.setattr(main, "ping_database", fail)
    res = client.get("/api/health/db")
    assert res.status_code == 503 and res.json() == {"ok": False, "error": "ConnectionError"}
    assert db.mongo.name not in res.text and not re.search(r"\d", res.text)


def test_health_stays_trivial(client, monkeypatch):
    monkeypatch.setattr(main, "ping_database", lambda _m: (_ for _ in ()).throw(ConnectionError()))
    assert client.get("/api/health").status_code == 200
