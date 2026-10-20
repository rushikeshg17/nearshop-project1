"""scripts/smoke.py against a fake NearShop HTTP server, and the commit on /api/health."""
import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SMOKE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "smoke.py"
_spec = importlib.util.spec_from_file_location("nearshop_smoke", SMOKE_PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

EMAIL = "smoke-account@example.test"
PASSWORD = "smoke-password-not-real-1234"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _group(product_id: int, quantity: int = 4) -> dict:
    offer = {"id": product_id, "quantity": quantity, "shop": {"id": 1, "offers_pickup": True}}
    return {"key": f"c{product_id}", "name": "Item", "best_offer": offer, "offers": [offer]}


class FakeNearShop(ThreadingHTTPServer):
    """Answers the endpoints the smoke test uses. Tests override answers per (method, path)."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.calls: list[tuple[str, str]] = []
        self.overrides: dict[tuple[str, str], tuple[int, object]] = {}
        self.delays: dict[tuple[str, str], float] = {}
        self.commits: list[str] = [COMMIT]  # successive /api/health answers; the last one repeats
        self.reservations: dict[int, str] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def answer(self, method: str, path: str, body: dict | None, cookie: str) -> tuple[int, object, list[str]]:
        key = (method, path)
        if key in self.delays:
            time.sleep(self.delays[key])
        if key in self.overrides:
            status, payload = self.overrides[key]
            return status, payload, []
        signed_in = "nearshop_session=token-1" in cookie
        if key == ("GET", "/api/health"):
            commit = self.commits.pop(0) if len(self.commits) > 1 else self.commits[0]
            return 200, {"ok": True, "commit": commit}, []
        if key == ("GET", "/api/meta"):
            return 200, {"city": {"name": "Ballari", "center": {"lat": 15.14, "lng": 76.92}},
                         "stats": {"shops": 3, "listings": 30, "in_stock": 20}}, []
        if key == ("GET", "/api/search"):
            return 200, {"total": 2, "groups": [_group(11), _group(12)]}, []
        if key == ("POST", "/api/auth/login"):
            if body == {"email": EMAIL, "password": PASSWORD}:
                return 200, {"id": 7, "role": "customer"}, ["nearshop_session=token-1; Path=/; HttpOnly"]
            return 401, {"error": {"code": "invalid_credentials", "message": "Email or password is incorrect"}}, []
        if key == ("POST", "/api/reservations"):
            if not signed_in:
                return 401, {"detail": "Please sign in to continue"}, []
            rid = 100 + len(self.reservations)
            self.reservations[rid] = "REQUESTED"
            return 200, {"id": rid, "code": f"R{rid}", "status": "REQUESTED"}, []
        if method == "POST" and path.startswith("/api/reservations/") and path.endswith("/cancel"):
            rid = int(path.split("/")[3])
            if not signed_in or rid not in self.reservations:
                return 404, {"error": {"code": "not_found", "message": "Reservation not found"}}, []
            self.reservations[rid] = "CANCELLED"
            return 200, {"id": rid, "status": "CANCELLED"}, []
        return 404, {"detail": "Not Found"}, []


class Handler(BaseHTTPRequestHandler):
    server: FakeNearShop

    def log_message(self, *_):  # keep pytest output clean
        pass

    def _handle(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.calls.append((self.command, path))
        if self.command != "GET" and self.headers.get("X-NearShop-Client") != "web":
            status, payload, cookies = 403, {"detail": "Missing client header"}, []
        else:
            status, payload, cookies = self.server.answer(self.command, path, body, self.headers.get("Cookie", ""))
        raw = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for c in cookies:
                self.send_header("Set-Cookie", c)
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):  # the client gave up after its timeout
            pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


@pytest.fixture
def server():
    srv = FakeNearShop()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("NEARSHOP_SMOKE_EMAIL", EMAIL)
    monkeypatch.setenv("NEARSHOP_SMOKE_PASSWORD", PASSWORD)
    monkeypatch.delenv("NEARSHOP_SMOKE_VERCEL_BYPASS", raising=False)


def run(server: FakeNearShop, *args: str) -> int:
    return smoke.main([server.url, "--timeout", "2", *args])


def methods(server: FakeNearShop) -> set[str]:
    return {m for m, _ in server.calls}


# ------------------------------------------------------------------ passing runs


def test_read_only_passes_and_prints_timings(server, creds, capsys):
    assert run(server, "--mode", "read-only") == 0
    out = capsys.readouterr().out
    assert "Smoke test passed" in out
    for step in ("health", "meta", *(f"search {q!r}" for q in smoke.SEARCHES)):
        assert f"ok    {step}" in out
    assert out.count(" ms ") >= 5  # every step prints its timing
    assert [p for _, p in server.calls].count("/api/search") == len(smoke.SEARCHES) == 3


def test_read_only_never_posts(server, creds):
    """Credentials in the environment must not make read-only mode sign in or reserve anything."""
    assert run(server, "--mode", "read-only") == 0
    assert methods(server) == {"GET"}
    assert not server.reservations


def test_read_only_is_the_default_mode(server, creds):
    assert run(server) == 0
    assert methods(server) == {"GET"}


def test_full_mode_reserves_and_cancels(server, creds, capsys):
    assert run(server, "--mode", "full") == 0
    assert server.reservations == {100: "CANCELLED"}
    assert ("POST", "/api/auth/login") in server.calls
    assert ("POST", "/api/reservations/100/cancel") in server.calls
    out = capsys.readouterr().out
    assert "ok    cancel" in out and "Smoke test passed" in out
    assert EMAIL not in out and PASSWORD not in out and "token-1" not in out


def test_full_mode_tries_the_next_listing_after_a_conflict(server, creds):
    calls = {"n": 0}
    original = server.answer

    def first_conflicts(method, path, body, cookie):
        if (method, path) == ("POST", "/api/reservations") and calls["n"] == 0:
            calls["n"] += 1
            return 409, {"error": {"code": "duplicate_reservation", "message": "already active"}}, []
        return original(method, path, body, cookie)

    server.answer = first_conflicts
    assert run(server, "--mode", "full") == 0
    assert list(server.reservations.values()) == ["CANCELLED"]


def test_waits_for_the_expected_commit(server, creds, capsys):
    server.commits = ["oldcommit000", "oldcommit000", COMMIT]
    assert run(server, "--expect-commit", COMMIT, "--poll-seconds", "0.05", "--wait-seconds", "5") == 0
    assert "ok    wait for commit" in capsys.readouterr().out


# ------------------------------------------------------------------ failing runs


@pytest.mark.parametrize("mode,key,status,payload", [
    ("read-only", ("GET", "/api/health"), 503, {"ok": False}),
    ("read-only", ("GET", "/api/health"), 200, {"ok": False}),
    ("read-only", ("GET", "/api/meta"), 500, {"detail": "Internal Server Error"}),
    ("read-only", ("GET", "/api/meta"), 200, {"stats": {}}),
    ("read-only", ("GET", "/api/search"), 200, {"total": 0, "groups": []}),
    ("read-only", ("GET", "/api/search"), 502, None),
    ("full", ("POST", "/api/auth/login"), 401, {"error": {"code": "invalid_credentials", "message": "no"}}),
    ("full", ("POST", "/api/reservations"), 409, {"error": {"code": "insufficient_stock", "message": "none"}}),
    ("full", ("POST", "/api/reservations"), 500, {"detail": "boom"}),
    ("full", ("POST", "/api/reservations/100/cancel"), 409, {"error": {"code": "invalid", "message": "no"}}),
    ("full", ("POST", "/api/reservations/100/cancel"), 200, {"id": 100, "status": "REQUESTED"}),
])
def test_any_failing_step_exits_non_zero(server, creds, capsys, mode, key, status, payload):
    server.overrides[key] = (status, payload)
    assert run(server, "--mode", mode) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "Smoke test FAILED" in out
    assert EMAIL not in out and PASSWORD not in out


def test_a_slow_step_times_out_and_fails(server, creds, capsys):
    server.delays[("GET", "/api/search")] = 1.5
    assert smoke.main([server.url, "--timeout", "0.3"]) == 1
    out = capsys.readouterr().out
    assert "timed out" in out
    assert "FAIL  search" in out


def test_commit_that_never_arrives_fails(server, creds, capsys):
    server.commits = ["oldcommit000"]
    assert run(server, "--expect-commit", COMMIT, "--poll-seconds", "0.05", "--wait-seconds", "0.3") == 1
    out = capsys.readouterr().out
    assert "not live after" in out
    assert ("GET", "/api/meta") not in server.calls  # nothing else runs against the old version


def test_full_mode_without_credentials_fails(server, monkeypatch):
    monkeypatch.delenv("NEARSHOP_SMOKE_EMAIL", raising=False)
    monkeypatch.delenv("NEARSHOP_SMOKE_PASSWORD", raising=False)
    assert run(server, "--mode", "full") == 1
    assert "POST" not in methods(server)


def test_unreachable_server_fails(creds, capsys):
    srv = FakeNearShop()
    url = srv.url
    srv.server_close()  # nothing listens on that port any more
    assert smoke.main([url, "--timeout", "1"]) == 1
    assert "FAIL  health" in capsys.readouterr().out


def test_bad_base_url_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        smoke.main(["nearshop-umber.vercel.app"])
    assert exc.value.code == 2


# ------------------------------------------------------------------ /api/health


def test_health_reports_the_render_commit(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", COMMIT)
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"ok": True, "commit": COMMIT}


def test_health_without_a_commit(monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"ok": True, "commit": None}
