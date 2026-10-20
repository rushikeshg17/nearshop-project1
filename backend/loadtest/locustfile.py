"""NearShop load test (P2.5; plan section 8.11). Staging only, through its Vercel URL: never production.

    cd backend && NEARSHOP_LOADTEST_CONFIRM_HOST=<staging host> uv run locust -f loadtest/locustfile.py \
        --headless --host https://<staging host> -u <30 + test accounts> -r 3 -t 10m

make loadtest (repository root) runs that, counting the accounts; docs/perf/baseline.md is the procedure and
the results.

Scenarios (one Locust user class each):
  SearchUser       anonymous search: types a query from queries.csv with three suggest-while-typing calls,
                   searches, then sometimes opens a product page (detail and recommendations) and a shop page
                   (detail, products and reviews), as the site's pages do. Think time NEARSHOP_LOADTEST_THINK
                   seconds ("5,15"). Now and then it also fetches /favicon.ico, a static file Vercel's edge
                   serves itself, to measure the network to the edge alone.
  OwnerUser        an owner's dashboard: GET /api/owner/overview every 15 s, as the site polls it.
  ReservationUser  a staging test customer reserves one unit of an in-stock pickup listing and cancels it,
                   once every NEARSHOP_LOADTEST_RESERVE_PACE seconds (360: the plan's 10 per customer per hour).
The owner and customer users sign in with staging test accounts from NEARSHOP_LOADTEST_ACCOUNTS, a CSV file
kept outside the repository with the columns role,email,password (role owner or customer). One user runs per
account; without the file both classes are off. Credentials are never printed or written anywhere.

Targets: 30 SearchUsers for 10 minutes. The owner and reservation users come on top (fixed counts), so
-u is 30 plus the number of accounts. NEARSHOP_LOADTEST_SHAPE=steps instead steps the search users through
NEARSHOP_LOADTEST_STEPS ("30,45,60,90,120,150,200"), NEARSHOP_LOADTEST_STEP_SECONDS (120) each, to find
the breaking point: the first step where end-to-end p95 exceeds 2 s or errors exceed 1%.

Measurements: every response's Server-Timing (app: time in the API; db; dbq: total wait of its gathered
queries in the mongo-io pool's queue) is recorded next to Locust's end-to-end time, and network is
end-to-end minus app. When the run stops, a summary is printed and written as JSON to
NEARSHOP_LOADTEST_SUMMARY (default ~/.nearshop/loadtest/summary-<UTC time>.json): p50, p95 and p99 per
endpoint, server-side and end to end, pool queue wait p95, error rates, the Vercel edge and function
regions seen (x-vercel-id), each step's p95 and error rate, and a pass or fail for each target.

Safety: the run refuses to start against the production site (nearshop-umber.vercel.app and every host in
NEARSHOP_PRODUCTION_URL, comma-separated), against a Render URL (go through Vercel, as users do), and
against any other host unless NEARSHOP_LOADTEST_CONFIRM_HOST names exactly that host. Local servers
(localhost, 127.0.0.1) need no confirmation, for trying the script. NEARSHOP_LOADTEST_VERCEL_BYPASS, when
set, is sent as Vercel's deployment-protection bypass header. Run one Locust process (no workers): the
summary is collected in-process.
"""
from __future__ import annotations

import csv
import itertools
import json
import logging
import math
import os
import random
import re
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import gevent
from locust import HttpUser, LoadTestShape, between, constant_pacing, events, task
from locust.exception import StopUser

log = logging.getLogger("nearshop.loadtest")

HERE = Path(__file__).resolve().parent
PRODUCTION_HOSTS = frozenset({"nearshop-umber.vercel.app"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CLIENT_HEADER = {"X-NearShop-Client": "web"}  # the API refuses a state-changing request without it
RADIUS_KM = 5
RESERVE_NOTE = "Load test; cancelled straight away."

# Targets (plan section 8.11).
SEARCH_USERS = 30
SERVER_SEARCH_P95_MS = 600
END_TO_END_P95_MS = 1200
MAX_ERROR_RATE = 0.005
POOL_QUEUE_P95_MS = 50
NETWORK_SHARE_MAX = 0.40
BREAK_P95_MS = 2000
BREAK_ERROR_RATE = 0.01
MIN_STEP_SAMPLES = 50

SEARCH = "GET /api/search"
SUGGEST = "GET /api/search/suggest"
EDGE = "GET /favicon.ico (edge only)"
LOGIN = "POST /api/auth/login"


# ------------------------------------------------------------------ configuration


def check_target(host: str | None, confirm: str | None, production: str | None) -> str | None:
    """Why the run must not start against host, or None when it may."""
    parts = urlsplit(host or "")
    name = (parts.hostname or "").lower()
    if not name:
        return "no --host given: pass the staging Vercel URL, for example --host https://<staging host>"
    refused = PRODUCTION_HOSTS | {(urlsplit(u.strip()).hostname or u.strip()).lower()
                                  for u in (production or "").split(",") if u.strip()}
    if name in refused:
        return f"{name} is production. Load-test staging only."
    if name in LOCAL_HOSTS:
        return None
    if name.endswith(".onrender.com"):
        return f"{name} is a Render URL. Load-test through the staging Vercel URL, as users reach the API."
    if parts.scheme != "https":
        return f"{host} is not https. Use the staging Vercel URL."
    if (confirm or "").strip().lower() != name:
        return (f"set NEARSHOP_LOADTEST_CONFIRM_HOST={name} to confirm that {name} is staging "
                "(resized to the production plan for the run)")
    return None


def load_queries(path: Path = HERE / "queries.csv") -> list[str]:
    with path.open(newline="", encoding="utf-8") as f:
        return [row["query"].strip() for row in csv.DictReader(f) if row.get("query", "").strip()]


def load_accounts(path: str | None) -> dict[str, list[tuple[str, str]]]:
    accounts: dict[str, list[tuple[str, str]]] = {"owner": [], "customer": []}
    if not path:
        return accounts
    with open(os.path.expanduser(path), newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            role = (row.get("role") or "").strip().lower()
            if role in accounts and row.get("email") and row.get("password"):
                accounts[role].append((row["email"].strip(), row["password"]))
    return accounts


def _pair(value: str | None, default: tuple[float, float]) -> tuple[float, float]:
    try:
        low, high = (float(x) for x in (value or "").split(","))
        return (low, high) if 0 <= low <= high else default
    except ValueError:
        return default


QUERIES = load_queries()
ACCOUNTS = load_accounts(os.environ.get("NEARSHOP_LOADTEST_ACCOUNTS"))
THINK = _pair(os.environ.get("NEARSHOP_LOADTEST_THINK"), (5.0, 15.0))
RESERVE_PACE = float(os.environ.get("NEARSHOP_LOADTEST_RESERVE_PACE") or 360)
_owner_accounts = itertools.cycle(ACCOUNTS["owner"] or [("", "")])
_customer_accounts = itertools.cycle(ACCOUNTS["customer"] or [("", "")])

# Set when the run starts: the target was checked, and the city centre the searches use.
state: dict[str, Any] = {"allowed": False, "center": None, "environment": None}


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "nearshop-loadtest/1", **(extra or {})}
    if bypass := os.environ.get("NEARSHOP_LOADTEST_VERCEL_BYPASS"):
        headers["x-vercel-protection-bypass"] = bypass
    return headers


# ------------------------------------------------------------------ measurements

_TIMING = re.compile(r"([A-Za-z0-9_-]+);dur=([\d.]+)")
samples: list[dict[str, Any]] = []


def server_timing(header: str | None) -> dict[str, float]:
    return {m.group(1): float(m.group(2)) for m in _TIMING.finditer(header or "")}


@events.init.add_listener
def _on_init(environment, **_kw) -> None:
    state["environment"] = environment


@events.request.add_listener
def _on_request(request_type, name, response_time, response_length, response=None, exception=None,
                start_time=None, **_kw) -> None:
    timing = server_timing(response.headers.get("server-timing")) if response is not None else {}
    vercel_id = response.headers.get("x-vercel-id", "") if response is not None else ""
    runner = getattr(state["environment"], "runner", None)
    samples.append({
        "name": name, "e2e": float(response_time), "app": timing.get("app"), "db": timing.get("db"),
        "dbq": timing.get("dbq"), "ok": exception is None,
        "status": getattr(response, "status_code", 0) or 0, "users": getattr(runner, "user_count", 0),
        "regions": "::".join(vercel_id.split("::")[:-1]) if "::" in vercel_id else "",
    })


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)], 1)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r["ok"]]
    e2e = [r["e2e"] for r in ok]
    app = [r["app"] for r in ok if r["app"] is not None]
    network = [r["e2e"] - r["app"] for r in ok if r["app"] is not None]
    return {
        "requests": len(rows), "errors": len(rows) - len(ok),
        "error_rate": round((len(rows) - len(ok)) / len(rows), 4) if rows else None,
        "end_to_end_ms": {f"p{q}": percentile(e2e, q) for q in (50, 95, 99)},
        "server_ms": {f"p{q}": percentile(app, q) for q in (50, 95, 99)},
        "network_ms": {f"p{q}": percentile(network, q) for q in (50, 95)},
        "db_ms_p95": percentile([r["db"] for r in ok if r["db"] is not None], 95),
        "pool_queue_ms_p95": percentile([r["dbq"] for r in ok if r["dbq"] is not None], 95),
    }


def _verdict(value: float | None, limit: float, *, strict: bool = False) -> str:
    if value is None:
        return "NOT MEASURED"
    return "PASS" if (value < limit if strict else value <= limit) else "FAIL"


def summarize(rows: list[dict[str, Any]], host: str | None) -> dict[str, Any]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_name[r["name"]].append(r)
    endpoints = {name: _stats(group) for name, group in sorted(by_name.items())}
    measured = [r for r in rows if r["name"] not in (LOGIN, EDGE)]
    overall = _stats(measured)
    search = endpoints.get(SEARCH) or _stats([])
    e2e95, app95 = search["end_to_end_ms"]["p95"], search["server_ms"]["p95"]
    network_share = round((e2e95 - app95) / e2e95, 3) if e2e95 and app95 is not None else None
    edge95 = (endpoints.get(EDGE) or _stats([]))["end_to_end_ms"]["p95"]

    by_users: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in measured:
        by_users[r["users"]].append(r)
    steps = []
    for users, group in sorted(by_users.items()):
        if len(group) >= MIN_STEP_SAMPLES:  # short ramps between steps are left out
            s = _stats(group)
            steps.append({"users": users, "requests": s["requests"], "error_rate": s["error_rate"],
                          "end_to_end_p95_ms": s["end_to_end_ms"]["p95"]})
    breaking = next((s["users"] for s in steps if (s["end_to_end_p95_ms"] or 0) > BREAK_P95_MS
                     or (s["error_rate"] or 0) > BREAK_ERROR_RATE), None)

    targets = {
        "server_search_p95_ms<=600": _verdict(app95, SERVER_SEARCH_P95_MS),
        "end_to_end_search_p95_ms<=1200": _verdict(e2e95, END_TO_END_P95_MS),
        "error_rate<0.5%": _verdict(overall["error_rate"], MAX_ERROR_RATE, strict=True),
        "pool_queue_wait_p95_ms<=50": _verdict(overall["pool_queue_ms_p95"], POOL_QUEUE_P95_MS),
        "peak_memory<=75%_of_RAM": "READ FROM RENDER METRICS",
    }
    stay = (e2e95 is not None and network_share is not None and e2e95 <= END_TO_END_P95_MS
            and network_share <= NETWORK_SHARE_MAX)
    return {
        "host": host, "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "peak_users": max((r["users"] for r in rows), default=0),
        "overall": overall, "endpoints": endpoints, "targets": targets,
        "region": {"search_end_to_end_p95_ms": e2e95, "search_server_p95_ms": app95,
                   "network_share_of_end_to_end_p95": network_share, "edge_only_p95_ms": edge95,
                   "rule": "stay in Singapore" if stay else "write the move recommendation",
                   "vercel_regions_seen": dict(Counter(r["regions"] for r in rows if r["regions"]))},
        "steps": steps, "breaking_point_users": breaking,
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"\nNearShop load test summary: {summary['host']}, peak {summary['peak_users']} users")
    print(f"{'endpoint':44} {'n':>6} {'err%':>6} {'e2e p50/p95/p99 ms':>22} {'server p50/p95/p99 ms':>24} "
          f"{'dbq p95':>8}")
    for name, s in summary["endpoints"].items():
        e, a = s["end_to_end_ms"], s["server_ms"]
        e2e, server = f"{e['p50']}/{e['p95']}/{e['p99']}", f"{a['p50']}/{a['p95']}/{a['p99']}"
        print(f"{name[:44]:44} {s['requests']:6d} {100 * (s['error_rate'] or 0):6.2f} {e2e:>22} {server:>24} "
              f"{s['pool_queue_ms_p95']!s:>8}")
    for target, verdict in summary["targets"].items():
        print(f"  {verdict:24} {target}")
    region = summary["region"]
    print(f"  region: network {region['network_share_of_end_to_end_p95']} of search end-to-end p95 "
          f"({region['search_end_to_end_p95_ms']} ms); {region['rule']}; seen {region['vercel_regions_seen']}")
    if summary["steps"]:
        breaking = summary["breaking_point_users"]
        print(f"  steps: {summary['steps']}; breaking point: "
              f"{breaking if breaking is not None else 'not reached up to ' + str(summary['steps'][-1]['users'])}")


@events.test_start.add_listener
def _on_test_start(environment, **_kw) -> None:
    problem = check_target(environment.host, os.environ.get("NEARSHOP_LOADTEST_CONFIRM_HOST"),
                           os.environ.get("NEARSHOP_PRODUCTION_URL"))
    if problem:
        log.error("load test refused: %s", problem)
        state["allowed"] = False
        environment.process_exit_code = 2
        if environment.runner is not None:
            gevent.spawn(environment.runner.quit)  # after start() returns: quitting inside it is ignored
        return
    state["allowed"] = True
    samples.clear()


@events.test_stop.add_listener
def _on_test_stop(environment, **_kw) -> None:
    if not state["allowed"] or not samples:
        return
    summary = summarize(samples, environment.host)
    _print_summary(summary)
    default = Path.home() / ".nearshop" / "loadtest" / f"summary-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out = Path(os.path.expanduser(os.environ.get("NEARSHOP_LOADTEST_SUMMARY") or default))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(f"Summary written to {out}")


# ------------------------------------------------------------------ users


class NearShopUser(HttpUser):
    """Every task starts with require_ready(): a refused target, or a failed setup, stops the user before it
    sends anything (Locust runs on_start and the first task before a quit takes effect)."""

    abstract = True
    ready = False

    def on_start(self) -> None:
        self.ready = self.prepare()

    def prepare(self) -> bool:
        if not state["allowed"]:
            return False
        self.client.headers.update(_headers())
        if state["center"] is None:
            with self.client.get("/api/meta", name="GET /api/meta", catch_response=True) as res:
                try:
                    center = res.json()["city"]["center"]
                    state["center"] = (float(center["lat"]), float(center["lng"]))
                except (ValueError, KeyError, TypeError):
                    res.failure("GET /api/meta has no city.center")
                    return False
        return True

    def require_ready(self) -> None:
        if not (self.ready and state["allowed"]):
            raise StopUser()

    def sign_in(self, account: tuple[str, str], role: str) -> bool:
        email, password = account
        with self.client.post("/api/auth/login", json={"email": email, "password": password},
                              headers=CLIENT_HEADER, name=LOGIN, catch_response=True) as res:
            body = res.json() if res.status_code == 200 else {}
            if res.status_code != 200 or body.get("role") != role:
                res.failure(f"sign-in as a {role} test account failed (HTTP {res.status_code})")
                return False
        return True

    def search(self, q: str) -> list[dict[str, Any]]:
        lat, lng = state["center"]
        with self.client.get("/api/search", params={"q": q, "lat": lat, "lng": lng, "radius_km": RADIUS_KM},
                             name=SEARCH, catch_response=True) as res:
            try:
                groups = res.json().get("groups") or []
            except ValueError:
                res.failure("search did not return JSON")
                return []
        return [g for g in groups if isinstance(g, dict)]

    @staticmethod
    def offers(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = []
        for group in groups:
            for offer in [group.get("best_offer"), *(group.get("offers") or [])]:
                if isinstance(offer, dict) and isinstance(offer.get("id"), int):
                    found.append(offer)
        return found


class SearchUser(NearShopUser):
    """An anonymous visitor: suggest while typing (three calls), search, then product and shop pages."""

    weight = 1
    wait_time = between(*THINK)

    def type_query(self, q: str) -> None:
        cuts = [max(2, math.ceil(len(q) * k / 3)) for k in (1, 2, 3)]  # three calls, as the debounced box
        for n in cuts:
            self.client.get("/api/search/suggest", params={"q": q[:n]}, name=SUGGEST)
            time.sleep(random.uniform(0.2, 0.5))

    @task
    def visit(self) -> None:
        self.require_ready()
        q = random.choice(QUERIES)
        self.type_query(q)
        offers = self.offers(self.search(q))
        lat, lng = state["center"]
        if offers and random.random() < 0.5:
            offer = random.choice(offers[:10])
            time.sleep(random.uniform(1, 3))
            self.client.get(f"/api/products/{offer['id']}", params={"lat": lat, "lng": lng},
                            name="GET /api/products/{id}")
            self.client.get(f"/api/products/{offer['id']}/recommendations", params={"lat": lat, "lng": lng},
                            name="GET /api/products/{id}/recommendations")
            slug = (offer.get("shop") or {}).get("slug")
            if slug and random.random() < 0.6:
                time.sleep(random.uniform(1, 3))
                self.client.get(f"/api/shops/{slug}", params={"lat": lat, "lng": lng}, name="GET /api/shops/{slug}")
                self.client.get(f"/api/shops/{slug}/products", params={"page_size": 60},
                                name="GET /api/shops/{slug}/products")
                self.client.get(f"/api/shops/{slug}/reviews", params={"page_size": 8},
                                name="GET /api/shops/{slug}/reviews")
        if random.random() < 0.1:
            self.client.get("/favicon.ico", name=EDGE, headers={"Accept": "*/*"})


class OwnerUser(NearShopUser):
    """A shop owner with the dashboard open: the overview, every 15 s."""

    abstract = not ACCOUNTS["owner"]
    fixed_count = len(ACCOUNTS["owner"])
    wait_time = constant_pacing(15)

    def on_start(self) -> None:
        self.ready = self.prepare() and self.sign_in(next(_owner_accounts), "owner")

    @task
    def overview(self) -> None:
        self.require_ready()
        self.client.get("/api/owner/overview", name="GET /api/owner/overview")


class ReservationUser(NearShopUser):
    """A test customer who reserves one in-stock pickup listing and cancels it straight away."""

    abstract = not ACCOUNTS["customer"]
    fixed_count = len(ACCOUNTS["customer"])
    wait_time = constant_pacing(RESERVE_PACE)

    def on_start(self) -> None:
        self.ready = self.prepare() and self.sign_in(next(_customer_accounts), "customer")

    @task
    def reserve_and_cancel(self) -> None:
        self.require_ready()
        offers = [o for o in self.offers(self.search(random.choice(QUERIES)))
                  if (o.get("quantity") or 0) > 0 and (o.get("shop") or {}).get("offers_pickup", True)]
        if not offers:
            return
        offer = random.choice(offers[:10])
        with self.client.post("/api/reservations", json={"product_id": offer["id"], "quantity": 1,
                                                         "note": RESERVE_NOTE},
                              headers=CLIENT_HEADER, name="POST /api/reservations", catch_response=True) as res:
            if res.status_code == 409:  # a listing just sold out, or a leftover active reservation
                res.success()
                return
            reservation_id = res.json().get("id") if res.status_code == 200 else None
        if isinstance(reservation_id, int):
            self.client.post(f"/api/reservations/{reservation_id}/cancel", json={"reason": RESERVE_NOTE},
                             headers=CLIENT_HEADER, name="POST /api/reservations/{id}/cancel")


# ------------------------------------------------------------------ breaking-point run

if os.environ.get("NEARSHOP_LOADTEST_SHAPE") == "steps":

    class StepLoad(LoadTestShape):
        """Search users stepped up level by level; the signed-in users stay at their fixed counts."""

        levels = [int(x) for x in (os.environ.get("NEARSHOP_LOADTEST_STEPS") or "30,45,60,90,120,150,200").split(",")]
        seconds = int(os.environ.get("NEARSHOP_LOADTEST_STEP_SECONDS") or 120)
        fixed = len(ACCOUNTS["owner"]) + len(ACCOUNTS["customer"])

        def tick(self) -> tuple[int, float] | None:
            step = int(self.get_run_time() // self.seconds)
            if step >= len(self.levels):
                return None
            return self.levels[step] + self.fixed, 5.0
