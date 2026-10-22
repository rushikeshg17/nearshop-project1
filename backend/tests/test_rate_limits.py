"""Edge-only access, the client's address and the rate limits of plan section 8.3 (P2.3).

The limiter runs in enforce mode here (the test environment's default is off, so other tests are never
limited), with every counter cleared before each test. Time is a controllable clock set in the future, so
MongoDB's TTL monitor never removes a counter during a test and no test sleeps."""
import io
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo.errors import PyMongoError
from starlette.requests import Request

from app import main
from app.core import ratelimit
from app.core.config import settings
from app.core.database import Database
from app.core.deps import ClientIP, get_client_ip
from app.core.security import create_access_token
from database import schema
from database.migrations import latest_number, m0003_rate_limits
from tests.conftest import HEADERS, PASSWORD

SECRET = "edge-secret-for-tests-" + "x" * 24
MINUTE, HOUR, DAY = 60, 3600, 86400


class Clock:
    def __init__(self) -> None:
        # The start of a UTC day 30 days ahead, plus a second: every hour of the day tests is the same day.
        day = (datetime.now(UTC) + timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.t = day.timestamp() + 1

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ratelimit, "now", c)
    return c


@pytest.fixture
def limits(ids, monkeypatch, clock):
    """Enforcement on, an edge secret configured (so tests can name the client's IP) but not required."""
    monkeypatch.setattr(settings, "rate_limit_mode", "enforce")
    monkeypatch.setattr(settings, "edge_secret", SecretStr(SECRET))
    monkeypatch.setattr(settings, "edge_secret_required", False)
    Database().mongo[ratelimit.COLLECTION].delete_many({})
    yield clock
    Database().mongo[ratelimit.COLLECTION].delete_many({})


@pytest.fixture
def api(limits):
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def request_log():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    from app.core import observability

    handler.setFormatter(observability.JsonFormatter())
    logger = logging.getLogger("nearshop.request")
    logger.addHandler(handler)
    yield buffer
    logger.removeHandler(handler)


def edge(ip: str = "203.0.113.10", **extra: str) -> dict[str, str]:
    """Headers as Vercel's proxy sends them: the secret and the visitor's address."""
    return {"X-NearShop-Edge": SECRET, "X-NearShop-Client-IP": ip, **HEADERS, **extra}


def bearer(email: str) -> dict[str, str]:
    user = Database().users.find_one({"email": email})
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role.value)}"}


def assert_limited(res, window: int) -> None:
    assert res.status_code == 429, res.text
    assert res.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(res.headers["retry-after"]) <= window


def run_to_limit(send, limit: int, window: int) -> None:
    """limit requests pass the limiter, the next is refused with Retry-After."""
    for n in range(limit):
        res = send()
        assert res.status_code != 429, f"request {n + 1} of {limit} was limited"
    assert_limited(send(), window)


# ---------------------------------------------------------------- every limit of plan section 8.3


def _customer(api):
    return dict(edge(), **bearer("cust@example.com"))


def _owner(api):
    return dict(edge(), **bearer("a@example.com"))


# (name, method, path, request kwargs given the headers, headers, limit, window): one counter each. Bodies are
# deliberately invalid where that is cheapest: the limiter counts the request before the route validates it.
SINGLE_WINDOW = [
    ("review create", "POST", "/api/reviews", {"json": {}}, _customer, 5, DAY),
    ("search per IP", "GET", "/api/search", {"params": {"radius_km": 0}}, lambda api: edge(), 120, MINUTE),
    ("search per session", "GET", "/api/search", {"params": {"radius_km": 0}}, _customer, 30, MINUTE),
    ("suggest", "GET", "/api/search/suggest", {"params": {"q": "x" * 81}}, lambda api: edge(), 240, MINUTE),
    ("search-compare", "GET", "/api/ai/search-compare", {"params": {"q": "x"}}, lambda api: edge(), 10, MINUTE),
    ("image upload", "POST", "/api/owner/shop/image", {}, _owner, 30, HOUR),
    ("import preview", "POST", "/api/owner/import/preview", {}, _owner, 20, HOUR),
    ("import commit", "POST", "/api/owner/import/commit", {"json": {}}, _owner, 10, HOUR),
    ("data export", "GET", "/api/me/export", {}, _customer, 3, DAY),
    ("catalogue read", "GET", "/api/products/999999", {}, lambda api: edge(), 300, MINUTE),
    ("default", "GET", "/api/auth/me", {}, lambda api: edge(), 600, MINUTE),
]


@pytest.mark.parametrize(("name", "method", "path", "kwargs", "headers", "limit", "window"), SINGLE_WINDOW,
                         ids=[case[0] for case in SINGLE_WINDOW])
def test_each_limit_refuses_one_over_and_resets_after_the_window(api, limits, name, method, path, kwargs, headers,
                                                                  limit, window):
    h = headers(api)

    def send():
        return api.request(method, path, headers=h, **kwargs)

    run_to_limit(send, limit, window)
    assert_limited(send(), window)  # still refused within the window
    limits.advance(window)
    assert send().status_code != 429


# (name, method, path, kwargs, headers, per hour, per day)
HOURLY_AND_DAILY = [
    ("registration", "POST", "/api/auth/register", {"json": {}}, lambda api: edge(), 3, 10),
    ("reservation create", "POST", "/api/reservations", {"json": {}}, _customer, 10, 30),
    ("order create", "POST", "/api/orders", {"json": {}}, _customer, 5, 15),
    ("password change", "POST", "/api/auth/password", {"json": {}}, _customer, 5, 10),
]


@pytest.mark.parametrize(("name", "method", "path", "kwargs", "headers", "hourly", "daily"), HOURLY_AND_DAILY,
                         ids=[case[0] for case in HOURLY_AND_DAILY])
def test_hourly_and_daily_limits(api, limits, name, method, path, kwargs, headers, hourly, daily):
    h = headers(api)

    def send():
        return api.request(method, path, headers=h, **kwargs)

    run_to_limit(send, hourly, HOUR)
    limits.advance(HOUR)  # the next hour of the same day
    sent = hourly
    while sent < daily:
        for _ in range(min(hourly, daily - sent)):
            assert send().status_code != 429
            sent += 1
        if sent < daily:
            assert_limited(send(), HOUR)
            limits.advance(HOUR)
    res = send()
    assert_limited(res, DAY)
    assert int(res.headers["retry-after"]) > HOUR  # the day's limit, not the hour's
    limits.advance(DAY)
    assert send().status_code != 429


def test_both_registration_routes_share_one_counter(api, limits):
    for _ in range(2):
        assert api.post("/api/auth/register", json={}, headers=edge()).status_code != 429
    assert api.post("/api/auth/register-shop", json={}, headers=edge()).status_code != 429
    assert_limited(api.post("/api/auth/register-shop", json={}, headers=edge()), HOUR)


def test_both_image_routes_share_one_counter(api, limits, ids):
    h = _owner(api)
    for _ in range(15):
        assert api.post("/api/owner/shop/image", headers=h).status_code != 429
        assert api.post(f"/api/owner/products/{ids['tape']}/image", headers=h).status_code != 429
    assert_limited(api.post("/api/owner/shop/image", headers=h), HOUR)


def test_per_user_limits_count_the_user_not_the_ip(api, limits):
    for n in range(5):
        res = api.post("/api/reviews", json={}, headers=dict(edge(f"198.51.100.{n}"), **bearer("cust@example.com")))
        assert res.status_code != 429
    assert_limited(api.post("/api/reviews", json={}, headers=dict(edge("198.51.100.99"),
                                                                    **bearer("cust@example.com"))), DAY)
    # another customer on the same IP has their own allowance
    assert api.post("/api/reviews", json={}, headers=dict(edge("198.51.100.99"),
                                                            **bearer("other@example.com"))).status_code != 429


def test_per_ip_limits_count_an_ipv6_address_by_its_64_network(api, limits):
    for n in range(10):
        res = api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge(f"2001:db8:1:2::{n + 1:x}"))
        assert res.status_code != 429
    assert_limited(api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge("2001:db8:1:2:ffff::1")), MINUTE)
    assert api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge("2001:db8:1:3::1")).status_code != 429


def test_admins_are_exempt_from_the_search_limits_only(api, limits):
    h = dict(edge(), **bearer("admin@example.com"))
    for _ in range(125):  # over both search limits (120 per IP, 30 per session)
        assert api.get("/api/search", params={"radius_km": 0}, headers=h).status_code != 429
    for _ in range(245):
        assert api.get("/api/search/suggest", params={"q": "x" * 81}, headers=h).status_code != 429
    run_to_limit(lambda: api.get("/api/ai/search-compare", params={"q": "x"}, headers=h), 10, MINUTE)
    # a customer on the same IP is limited by the search counter the admin never touched
    customer = dict(edge(), **bearer("cust@example.com"))
    run_to_limit(lambda: api.get("/api/search", params={"radius_km": 0}, headers=customer), 30, MINUTE)


def test_an_admin_search_still_counts_against_the_default(api, limits, monkeypatch):
    monkeypatch.setattr(ratelimit, "DEFAULT", ratelimit.Rule("default", 3, MINUTE, (ratelimit.IP,)))
    h = dict(edge(), **bearer("admin@example.com"))
    run_to_limit(lambda: api.get("/api/search", params={"radius_km": 0}, headers=h), 3, MINUTE)


# ---------------------------------------------------------------- the table


def test_routes_missing_from_the_table_get_the_default():
    for method, path in (("GET", "/api/auth/me"), ("POST", "/api/auth/logout"), ("GET", "/api/owner/overview"),
                         ("GET", "/api/no-such-route"), ("GET", "/docs"), ("GET", "/media/products/x.webp"),
                         ("POST", "/api/orders/quote"), ("GET", "/api/search/discover")):
        assert ratelimit.match(method, path) is None, path
    assert ratelimit.DEFAULT.limit == 600 and ratelimit.DEFAULT.per == MINUTE and ratelimit.DEFAULT.key == ("ip",)


def test_every_catalogue_read_and_meta_share_the_catalogue_limit():
    from app.api.routes import catalog

    paths = ["/api/meta", "/api/products/1", "/api/products/1/recommendations", "/api/shops", "/api/shops/shop-a",
             "/api/shops/shop-a/products", "/api/shops/shop-a/reviews"]
    for path in paths:
        row = ratelimit.match("GET", path)
        assert row is not None and row.rules == (ratelimit.CATALOG_READ,), path
    # the router row covers each GET route of the catalogue router, including routes added later (P2.8)
    covered = {row.template for row in ratelimit.compiled_rows() if row.rules == (ratelimit.CATALOG_READ,)}
    assert {"/api" + r.path for r in catalog.router.routes if "GET" in r.methods} <= covered
    assert ratelimit.match("HEAD", "/api/shops") is not None  # HEAD counts as GET


def test_every_row_names_a_real_route():
    paths = main.app.openapi()["paths"]
    for row in ratelimit.compiled_rows():
        if row.template in ratelimit.PLANNED_ROUTES:
            continue
        assert row.template in paths, row.template
        assert row.method.lower() in paths[row.template], (row.method, row.template)


def test_the_limits_match_plan_section_8_3():
    r = ratelimit
    assert (r.LOGIN_ACCOUNT_IP.limit, r.LOGIN_ACCOUNT_IP.per, r.LOGIN_ACCOUNT_IP.key) == (5, 900, ("body:email", "ip"))
    assert (r.LOGIN_IP.limit, r.LOGIN_IP.per) == (20, 900)
    assert (r.LOGIN_ACCOUNT.limit, r.LOGIN_ACCOUNT.per, r.LOGIN_ACCOUNT.cooldown) == (10, HOUR, 1800)
    assert all(rule.failures_only for rule in (r.LOGIN_ACCOUNT_IP, r.LOGIN_IP, r.LOGIN_ACCOUNT))
    assert (r.PASSWORD_CHANGE_HOUR.limit, r.PASSWORD_CHANGE_HOUR.per, r.PASSWORD_CHANGE_HOUR.key) == (5, HOUR, ("user",))
    assert (r.PASSWORD_CHANGE_DAY.limit, r.PASSWORD_CHANGE_DAY.per) == (10, DAY)
    assert not (r.PASSWORD_CHANGE_HOUR.failures_only or r.PASSWORD_CHANGE_DAY.failures_only)
    assert {rule.name for rows in r.LIMITS for rule in rows.rules if rule.admin_exempt} == {
        "search_ip", "search_session", "suggest_ip"}


# ---------------------------------------------------------------- password change


def test_guessing_the_current_password_with_a_held_session_is_limited(api, limits):
    """A wrong current password answers 400 (not a counted login failure), so the password-change rule counts
    every attempt by the account: a stolen session cookie cannot guess at the default 600 per minute."""
    res = api.post("/api/auth/login", json={"email": "cust@example.com", "password": PASSWORD}, headers=edge())
    assert res.status_code == 200
    assert api.cookies.get(settings.cookie_name)  # this client now holds the session cookie
    h = edge("198.51.100.7")

    def guess(current: str = "not-the-password"):
        return api.post("/api/auth/password", json={"current_password": current, "new_password": "x" * 20},
                        headers=h)

    for _ in range(5):
        res = guess()
        assert res.status_code == 400 and res.json()["error"]["code"] == "wrong_password"
    assert_limited(guess(), HOUR)
    assert_limited(guess(PASSWORD), HOUR)  # the right password too, until the window ends
    # another IP does not escape it: the count is the account's
    h = edge("198.51.100.8")
    assert_limited(guess(), HOUR)


def test_guessing_the_password_through_account_deletion_is_limited(api, limits):
    """Account deletion checks the password as well, so it counts against the same per-account budget: a stolen
    session cookie cannot guess there at the default rate, nor double its guesses by using both routes."""
    res = api.post("/api/auth/login", json={"email": "cust@example.com", "password": PASSWORD}, headers=edge())
    assert res.status_code == 200
    h = edge("198.51.100.9")

    def delete(password: str = "not-the-password"):
        return api.post("/api/auth/delete", json={"password": password}, headers=h)

    for _ in range(3):
        res = delete()
        assert res.status_code == 400 and res.json()["error"]["code"] == "wrong_password"
    for _ in range(2):  # the password-change route draws on the same budget
        res = api.post("/api/auth/password", json={"current_password": "nope", "new_password": "x" * 20}, headers=h)
        assert res.status_code == 400
    assert_limited(delete(), HOUR)
    assert_limited(delete(PASSWORD), HOUR)  # the right password too: the account is not deleted
    assert api.get("/api/auth/me", headers=h).json()["email"] == "cust@example.com"


# ---------------------------------------------------------------- password login


def _login(api, email: str, password: str = "wrong-password", ip: str = "203.0.113.20"):
    return api.post("/api/auth/login", json={"email": email, "password": password}, headers=edge(ip))


def test_the_sixth_failed_login_for_one_account_from_one_ip_gets_429(api, limits):
    for _ in range(5):
        assert _login(api, "cust@example.com").status_code == 401
    res = _login(api, "cust@example.com")
    assert_limited(res, 15 * MINUTE)
    assert_limited(_login(api, "CUST@example.com ", PASSWORD), 15 * MINUTE)  # the right password too, any case
    assert _login(api, "cust@example.com", ip="203.0.113.21").status_code == 401  # another IP: its own count
    limits.advance(15 * MINUTE)
    assert _login(api, "cust@example.com", PASSWORD).status_code == 200


def test_login_counts_only_failures(api, limits):
    for _ in range(8):
        assert _login(api, "cust@example.com", PASSWORD).status_code == 200
    for _ in range(8):  # an invalid body is not a failed login
        assert api.post("/api/auth/login", json={"email": "cust@example.com"}, headers=edge()).status_code == 422
    for _ in range(5):
        assert _login(api, "cust@example.com").status_code == 401
    assert_limited(_login(api, "cust@example.com"), 15 * MINUTE)


def test_twenty_failed_logins_per_ip_in_15_minutes(api, limits):
    for n in range(20):  # 4 accounts, 5 failures each: under the per-account limits
        assert _login(api, f"nobody{n // 5}@example.com").status_code == 401
    assert_limited(_login(api, "someone-else@example.com"), 15 * MINUTE)
    assert _login(api, "someone-else@example.com", ip="203.0.113.99").status_code == 401


def test_ten_failures_per_account_per_hour_then_a_30_minute_cooldown(api, limits):
    for n in range(10):  # from two IPs, under the per-account-and-IP limit of 5
        assert _login(api, "cust@example.com", ip=f"203.0.113.{30 + n // 5}").status_code == 401
    res = _login(api, "cust@example.com", PASSWORD, ip="203.0.113.40")
    assert_limited(res, 30 * MINUTE)
    assert int(res.headers["retry-after"]) == 30 * MINUTE
    limits.advance(29 * MINUTE)
    assert_limited(_login(api, "cust@example.com", PASSWORD, ip="203.0.113.41"), MINUTE)
    limits.advance(MINUTE)
    assert _login(api, "cust@example.com", PASSWORD, ip="203.0.113.42").status_code == 200
    assert _login(api, "cust@example.com", ip="203.0.113.43").status_code == 401  # counting starts afresh


def _padded_login(api, email: str, password: str = "wrong-password", ip: str = "203.0.113.20"):
    """A login whose JSON body is valid but larger than the limiter reads."""
    content = json.dumps({"email": email, "password": password}) + " " * (ratelimit.MAX_BODY_BYTES + 4000)
    return api.post("/api/auth/login", content=content,
                    headers=edge(ip, **{"Content-Type": "application/json"}))


def test_a_padded_body_cannot_escape_the_per_account_limits(api, limits):
    for n in range(10):  # each from its own IP; all count under the one fallback key for unreadable bodies
        assert _padded_login(api, "cust@example.com", ip=f"203.0.113.{100 + n}").status_code == 401
    assert_limited(_padded_login(api, "cust@example.com", PASSWORD, ip="203.0.113.120"), 30 * MINUTE)
    assert_limited(_padded_login(api, "other@example.com", ip="203.0.113.121"), 30 * MINUTE)
    assert _login(api, "cust@example.com", PASSWORD, ip="203.0.113.122").status_code == 200  # a normal body


def test_a_padded_body_counts_per_account_and_ip_too(api, limits):
    for _ in range(5):
        assert _padded_login(api, "cust@example.com").status_code == 401
    assert_limited(_padded_login(api, "someone@example.com"), 15 * MINUTE)


def test_a_body_that_is_not_json_still_counts(api, limits):
    for n in range(10):
        res = api.post("/api/auth/login", content=b"\xff" * 10, headers=edge(f"203.0.113.{100 + n}"))
        assert res.status_code == 422
    # Not failures, so nothing is held against the fallback key.
    assert _padded_login(api, "cust@example.com", PASSWORD, ip="203.0.113.130").status_code == 200


def test_variant_spellings_of_one_email_share_its_counters(api, limits):
    variants = ["cust@example.com", "CUST@EXAMPLE.COM", "cust@\uff45xample.com", " Cust@Example.com",
                "cust@ex\uff41mple.com"]
    for email in variants:  # all of them sign in to the one account
        assert _login(api, email).status_code == 401
    assert_limited(_login(api, "cust@\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.com", PASSWORD), 15 * MINUTE)


def test_concurrent_failed_logins_cannot_pass_the_limit_together(api, limits):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=40) as pool:
        codes = list(pool.map(lambda n: _login(api, "cust@example.com", ip=f"198.51.100.{n}").status_code,
                              range(40)))
    assert codes.count(401) <= 10 and codes.count(401) + codes.count(429) == 40
    assert_limited(_login(api, "cust@example.com", PASSWORD, ip="198.51.100.200"), 30 * MINUTE)


def test_attempts_in_flight_are_counted_before_they_are_served(limits, db):
    store = ratelimit.Counters(db.mongo[ratelimit.COLLECTION])
    hit = ratelimit.Hit(ratelimit.LOGIN_ACCOUNT, "digest-of-an-account")
    ts = limits()
    attempts = [store.check([], [hit], ts, refuse=True) for _ in range(40)]  # none answered yet
    admitted = [counts for over, counts in attempts if not over]
    assert len(admitted) == 10
    assert all(over[0][0] is ratelimit.LOGIN_ACCOUNT for over, _ in attempts if over)
    for counts in admitted[:9]:  # nine succeed: taken back
        store.settle([hit], counts, ts, failed=False, at=ts)
    over, counts = store.check([], [hit], ts, refuse=True)
    assert not over  # room again for one more
    store.settle([hit], admitted[9], ts, failed=True, at=ts)  # the tenth attempt fails: the cooldown starts
    over, _ = store.check([], [hit], ts, refuse=True)
    assert over and over[0][1] == 30 * MINUTE


def test_an_attempt_that_ends_without_an_answer_is_taken_back(api, limits, monkeypatch):
    from app.api.routes import auth

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", boom)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        for _ in range(12):  # past every login limit, had they been kept
            assert _login(client, "cust@example.com").status_code == 500
    monkeypatch.setattr(auth, "verify_password", real)
    assert _login(api, "cust@example.com", PASSWORD).status_code == 200


def test_counters_hold_no_address_or_email(api, limits, db):
    _login(api, "cust@example.com", ip="203.0.113.77")
    api.get("/api/search", params={"radius_km": 0}, headers=edge("203.0.113.77"))
    docs = list(db.mongo[ratelimit.COLLECTION].find())
    assert docs and all(isinstance(d["expires_at"], datetime) for d in docs)
    text = json.dumps(docs, default=str)
    assert "203.0.113.77" not in text and "cust@example.com" not in text and "@" not in text


# ---------------------------------------------------------------- edge-only access


@pytest.fixture
def edge_required(limits, monkeypatch):
    monkeypatch.setattr(settings, "edge_secret_required", True)


def test_a_missing_edge_secret_gets_403_and_health_checks_are_exempt(api, edge_required):
    for path in ("/api/meta", "/api/health/db", "/docs", "/openapi.json", "/media/products/x.webp", "/api/nowhere"):
        res = api.get(path)
        assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden", path
    assert api.get("/api/meta", headers={"X-NearShop-Edge": SECRET[:-1] + "?"}).status_code == 403
    assert api.post("/api/auth/login", json={}, headers=HEADERS).status_code == 403
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/health/ready").status_code in (200, 503)  # readiness, never refused at the edge
    assert api.get("/api/meta", headers=edge()).status_code == 200


def test_the_api_refuses_to_start_without_the_secret_when_it_is_required(limits, monkeypatch):
    monkeypatch.setattr(settings, "edge_secret_required", True)
    monkeypatch.setattr(settings, "edge_secret", SecretStr(""))
    with pytest.raises(RuntimeError, match="NEARSHOP_EDGE_SECRET"), TestClient(main.app):
        pass


def test_staging_and_production_require_the_secret_by_default():
    from app.core.config import Settings

    for environment, required, mode in (("production", True, "log_only"), ("staging", True, "log_only"),
                                        ("development", False, "enforce"), ("test", False, "off")):
        s = Settings(_env_file=None, environment=environment, secret_key="k" * 40)
        assert (s.edge_secret_required, s.rate_limit_mode) == (required, mode), environment
    assert Settings(_env_file=None, environment="production", rate_limit_mode="enforce").rate_limit_mode == "enforce"
    with pytest.raises(ValueError, match="32 or more"):
        Settings(_env_file=None, edge_secret="short")
    with pytest.raises(ValueError, match="NEARSHOP_RATE_LIMIT_MODE"):
        Settings(_env_file=None, rate_limit_mode="sometimes")


@pytest.fixture
def echo_route():
    """A test route that shows what the app sees of the client."""

    def echo(request: Request, ip: ClientIP):
        return {"ip": ip, "edge": request.headers.get("x-nearshop-edge"),
                "client_ip_header": request.headers.get("x-nearshop-client-ip")}

    main.app.add_api_route("/api/test-ratelimit-echo", echo, methods=["GET"])
    yield
    main.app.router.routes[:] = [r for r in main.app.router.routes
                                 if getattr(r, "path", None) != "/api/test-ratelimit-echo"]


def test_the_client_ip_header_is_believed_only_with_the_secret(api, limits, echo_route, request_log):
    spoofed = api.get("/api/test-ratelimit-echo", headers={"X-NearShop-Client-IP": "192.0.2.1"}).json()
    assert spoofed == {"ip": "testclient", "edge": None, "client_ip_header": None}
    wrong = api.get("/api/test-ratelimit-echo", headers={"X-NearShop-Edge": "nope" * 10,
                                                         "X-NearShop-Client-IP": "192.0.2.1"}).json()
    assert wrong["ip"] == "testclient"
    trusted = api.get("/api/test-ratelimit-echo", headers=edge("192.0.2.55")).json()
    assert trusted == {"ip": "192.0.2.55", "edge": None, "client_ip_header": None}  # the app never sees either
    not_an_ip = api.get("/api/test-ratelimit-echo", headers=edge("not-an-ip")).json()
    assert not_an_ip["ip"] == "testclient"
    lines = [json.loads(line) for line in request_log.getvalue().splitlines()]
    assert [line["client_ip"] for line in lines] == ["testclient", "testclient", "192.0.2.55", "testclient"]


def test_a_client_ip_header_without_the_secret_does_not_change_the_counter(api, limits):
    for n in range(10):
        res = api.get("/api/ai/search-compare", params={"q": "x"}, headers={"X-NearShop-Client-IP": f"192.0.2.{n}"})
        assert res.status_code != 429
    res = api.get("/api/ai/search-compare", params={"q": "x"}, headers={"X-NearShop-Client-IP": "192.0.2.200"})
    assert_limited(res, MINUTE)


def test_server_rendered_calls_count_against_the_visitor(api, limits):
    """P2.8: the Next.js server calls with the secret and the visitor's IP, from one address for everyone."""
    for _ in range(10):
        assert api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge("198.51.100.1")).status_code != 429
    assert_limited(api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge("198.51.100.1")), MINUTE)
    assert api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge("198.51.100.2")).status_code != 429


def test_get_client_ip_falls_back_to_the_peer():
    scope = {"type": "http", "headers": [], "client": ("192.0.2.9", 1234), "state": {}}
    assert get_client_ip(Request(scope)) == "192.0.2.9"
    scope["state"]["client_ip"] = "198.51.100.3"
    assert get_client_ip(Request(scope)) == "198.51.100.3"


# ---------------------------------------------------------------- modes and failures


def test_log_only_mode_never_blocks(api, limits, monkeypatch, request_log):
    monkeypatch.setattr(settings, "rate_limit_mode", "log_only")
    for _ in range(12):
        assert api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge()).status_code == 422
    for _ in range(12):
        assert _login(api, "cust@example.com").status_code == 401
    lines = [json.loads(line) for line in request_log.getvalue().splitlines()]
    flagged = [line for line in lines if "rate_limited" in line]
    # 2 searches over 10; failed logins 6 to 10 over 5 per account and IP, and 11 and 12 also in the cooldown
    assert [set(line["rate_limited"].split(",")) for line in flagged] == (
        [{"search_compare_ip"}] * 2 + [{"login_account_ip"}] * 5 + [{"login_account_ip", "login_account"}] * 2)
    assert all(line["rate_limit_mode"] == "log_only" and line["client_ip"] == "203.0.113.20" for line in flagged[2:])


def test_off_mode_counts_nothing(api, limits, monkeypatch, db):
    monkeypatch.setattr(settings, "rate_limit_mode", "off")
    for _ in range(12):
        assert api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge()).status_code == 422
    assert db.mongo[ratelimit.COLLECTION].count_documents({}) == 0


def test_the_request_is_served_when_the_counters_cannot_be_reached(api, limits, monkeypatch):
    def down(*_args, **_kwargs):
        raise PyMongoError("no primary")

    monkeypatch.setattr(ratelimit.Counters, "_incr", down)
    monkeypatch.setattr(ratelimit.Counters, "_cooldowns", down)
    for _ in range(12):
        assert api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge()).status_code == 422
    assert _login(api, "cust@example.com").status_code == 401


def test_the_limit_answer_has_cors_headers(api, limits):
    for _ in range(10):
        api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge())
    res = api.get("/api/ai/search-compare", params={"q": "x"}, headers=edge(Origin="http://localhost:3000"))
    assert res.status_code == 429 and res.headers["access-control-allow-origin"] == "http://localhost:3000"


# ---------------------------------------------------------------- the collection


def test_migration_3_creates_rate_limits_with_a_ttl_index_and_is_idempotent(ids):
    from app.core.database import client

    scratch = client[f"{Database().mongo.name}_m3"]
    try:
        m0003_rate_limits.apply(scratch)
        m0003_rate_limits.apply(scratch)
        info = next(iter(scratch.list_collections(filter={"name": "rate_limits"})))
        assert info["options"]["validator"] == schema.VALIDATORS["rate_limits"]
        ttl = scratch["rate_limits"].index_information()["expires_ttl"]
        assert ttl["key"] == [("expires_at", 1)] and ttl["expireAfterSeconds"] == 0
        assert latest_number() >= m0003_rate_limits.NUMBER
    finally:
        client.drop_database(scratch.name)
