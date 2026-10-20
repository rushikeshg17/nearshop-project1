"""Smoke test for a deployed NearShop environment, run through its Vercel URL.

    uv run python scripts/smoke.py https://<vercel-url> --mode read-only
    uv run python scripts/smoke.py https://<staging-vercel-url> --mode full --expect-commit <sha>

read-only: GET /api/health, GET /api/meta, and three fixed searches that must each return at least one
result group. It never sends a POST, so it is safe against production.

full: everything in read-only, then signs in with the staging smoke account (NEARSHOP_SMOKE_EMAIL and
NEARSHOP_SMOKE_PASSWORD), reserves one unit of an in-stock pickup listing from the search results and
cancels that reservation. Staging only.

--expect-commit first waits (up to --wait-seconds) until /api/health reports that git commit, which Render
sets on each deploy. NEARSHOP_SMOKE_VERCEL_BYPASS, when set, is sent as Vercel's deployment-protection
bypass header, for preview deployments behind Vercel Authentication.

Prints the timing of each step. Exits 0 when every step passes and 1 on the first failure or timeout
(2 for bad arguments). Uses only the standard library, so it runs without the backend's dependencies.
It never prints the smoke account's credentials, the session cookie or a response's personal fields.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

SEARCHES = ("phone charger", "notebook", "umbrella")  # fixed queries; each has listings in every seeded city
RADIUS_KM = 5
CLIENT_HEADER = {"X-NearShop-Client": "web"}  # the API refuses a state-changing request without it
SMOKE_NOTE = "Automated smoke test; cancelled straight away."
MAX_RESERVE_ATTEMPTS = 5


class StepFailed(Exception):
    pass


class Client:
    """A tiny JSON client with one session cookie jar. Every request goes through request()."""

    def __init__(self, base_url: str, timeout: float, extra_headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.cookies: dict[str, str] = {}
        self.methods: list[str] = []  # every method sent, in order (the tests read it)

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None
                ) -> tuple[int, Any]:
        url = self.base_url + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
        headers = {"Accept": "application/json", "User-Agent": "nearshop-smoke/1", **self.extra_headers}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers.update(CLIENT_HEADER)
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        self.methods.append(method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                status, raw, set_cookies = res.status, res.read(), res.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            status, raw, set_cookies = exc.code, exc.read(), exc.headers.get_all("Set-Cookie") or []
        except TimeoutError as exc:
            raise StepFailed(f"{method} {path} timed out after {self.timeout:g} s") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise StepFailed(f"{method} {path} timed out after {self.timeout:g} s") from exc
            raise StepFailed(f"{method} {path} failed: {type(reason).__name__}: {reason}") from exc
        for header in set_cookies:
            name, _, rest = header.partition("=")
            value = rest.split(";", 1)[0]
            if value and "max-age=0" not in header.lower():
                self.cookies[name.strip()] = value
            else:
                self.cookies.pop(name.strip(), None)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload


def _error_text(payload: Any) -> str:
    """The API's error code and message only; never the rest of a body (it may hold personal fields)."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return f"{err.get('code', 'error')}: {str(err.get('message', ''))[:200]}"
        if "detail" in payload:
            return str(payload["detail"])[:200]
    return "no JSON error body"


def expect_ok(status: int, payload: Any, what: str) -> Any:
    if status != 200:
        raise StepFailed(f"{what} returned HTTP {status} ({_error_text(payload)})")
    if payload is None:
        raise StepFailed(f"{what} did not return JSON")
    return payload


class Smoke:
    def __init__(self, client: Client, mode: str, expect_commit: str | None = None, wait_seconds: float = 600,
                 poll_seconds: float = 15, email: str | None = None, password: str | None = None,
                 out=None):
        self.client = client
        self.mode = mode
        self.expect_commit = expect_commit
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.email = email
        self.password = password
        self.out = out
        self.center: tuple[float, float] | None = None
        self.groups: list[dict] = []
        self.reservation_id: int | None = None

    def say(self, line: str) -> None:
        print(line, file=self.out or sys.stdout, flush=True)

    # ------------------------------------------------------------------ steps

    def wait_for_commit(self) -> str:
        want = self.expect_commit.strip().lower()
        deadline = time.monotonic() + self.wait_seconds
        last = "no answer yet"
        while True:
            try:
                status, payload = self.client.request("GET", "/api/health")
                got = str((payload or {}).get("commit") or "").lower() if isinstance(payload, dict) else ""
                if status == 200 and got and (got.startswith(want) or want.startswith(got)):
                    return f"serving {got[:12]}"
                last = f"HTTP {status}, commit {got[:12] or 'not reported'}"
            except StepFailed as exc:
                last = str(exc)
            if time.monotonic() + self.poll_seconds > deadline:
                raise StepFailed(f"commit {want[:12]} not live after {self.wait_seconds:g} s (last: {last})")
            time.sleep(self.poll_seconds)

    def health(self) -> str:
        payload = expect_ok(*self.client.request("GET", "/api/health"), "GET /api/health")
        if payload.get("ok") is not True:
            raise StepFailed("GET /api/health did not report ok")
        return f"commit {str(payload.get('commit') or 'not reported')[:12]}"

    def meta(self) -> str:
        payload = expect_ok(*self.client.request("GET", "/api/meta"), "GET /api/meta")
        try:
            center = payload["city"]["center"]
            self.center = (float(center["lat"]), float(center["lng"]))
            listings = int(payload["stats"]["listings"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StepFailed("GET /api/meta is missing city.center or stats.listings") from exc
        return f"{payload['city'].get('name', 'city')}, {listings} listings"

    def search(self, q: str) -> str:
        lat, lng = self.center
        payload = expect_ok(*self.client.request(
            "GET", "/api/search", params={"q": q, "lat": lat, "lng": lng, "radius_km": RADIUS_KM}),
            f"search {q!r}")
        groups = payload.get("groups")
        if not isinstance(groups, list) or not groups:
            raise StepFailed(f"search {q!r} returned no result groups")
        self.groups.extend(g for g in groups if isinstance(g, dict))
        return f"{len(groups)} groups"

    def sign_in(self) -> str:
        if not self.email or not self.password:
            raise StepFailed("full mode needs NEARSHOP_SMOKE_EMAIL and NEARSHOP_SMOKE_PASSWORD")
        payload = expect_ok(*self.client.request(
            "POST", "/api/auth/login", body={"email": self.email, "password": self.password}), "sign-in")
        if payload.get("role") not in (None, "customer"):
            raise StepFailed("the smoke account must be a customer account")
        if not self.client.cookies:
            raise StepFailed("sign-in did not set a session cookie")
        return "signed in"

    def _candidates(self) -> list[int]:
        seen: list[int] = []
        for group in self.groups:
            for offer in [group.get("best_offer"), *(group.get("offers") or [])]:
                if not isinstance(offer, dict) or not isinstance(offer.get("id"), int):
                    continue
                shop = offer.get("shop") or {}
                if (offer.get("quantity") or 0) > 0 and shop.get("offers_pickup", True) and offer["id"] not in seen:
                    seen.append(offer["id"])
        return seen

    def reserve(self) -> str:
        candidates = self._candidates()
        if not candidates:
            raise StepFailed("no in-stock pickup listing in the search results to reserve")
        last = ""
        for product_id in candidates[:MAX_RESERVE_ATTEMPTS]:
            status, payload = self.client.request(
                "POST", "/api/reservations", body={"product_id": product_id, "quantity": 1, "note": SMOKE_NOTE})
            if status == 200 and isinstance(payload, dict) and isinstance(payload.get("id"), int):
                self.reservation_id = payload["id"]
                return f"reservation {payload.get('code') or payload['id']} ({payload.get('status')})"
            last = f"HTTP {status} ({_error_text(payload)})"
            if status != 409:  # 409: a leftover active reservation or no stock; try the next listing
                break
        raise StepFailed(f"could not create a reservation: {last}")

    def cancel(self) -> str:
        payload = expect_ok(*self.client.request(
            "POST", f"/api/reservations/{self.reservation_id}/cancel", body={"reason": SMOKE_NOTE}),
            "cancel reservation")
        if str(payload.get("status", "")).upper() != "CANCELLED":
            raise StepFailed(f"cancelled reservation has status {payload.get('status')!r}")
        return "cancelled"

    # ------------------------------------------------------------------ runner

    def steps(self) -> list[tuple[str, Callable[[], str]]]:
        steps: list[tuple[str, Callable[[], str]]] = []
        if self.expect_commit:
            steps.append((f"wait for commit {self.expect_commit[:12]}", self.wait_for_commit))
        steps += [("health", self.health), ("meta", self.meta)]
        steps += [(f"search {q!r}", lambda q=q: self.search(q)) for q in SEARCHES]
        if self.mode == "full":
            steps += [("sign in", self.sign_in), ("reserve", self.reserve), ("cancel", self.cancel)]
        return steps

    def run(self) -> int:
        self.say(f"NearShop smoke test, {self.mode} mode, against {self.client.base_url}")
        started = time.perf_counter()
        for name, step in self.steps():
            t0 = time.perf_counter()
            try:
                detail = step()
            except StepFailed as exc:
                self.say(f"FAIL  {name:<28} {(time.perf_counter() - t0) * 1000:8.0f} ms  {exc}")
                self.say(f"Smoke test FAILED after {time.perf_counter() - started:.1f} s.")
                return 1
            except Exception as exc:  # an unexpected response shape is a failure, not a crash
                self.say(f"FAIL  {name:<28} {(time.perf_counter() - t0) * 1000:8.0f} ms  "
                         f"{type(exc).__name__}: {str(exc)[:200]}")
                self.say(f"Smoke test FAILED after {time.perf_counter() - started:.1f} s.")
                return 1
            self.say(f"ok    {name:<28} {(time.perf_counter() - t0) * 1000:8.0f} ms  {detail}")
        self.say(f"Smoke test passed in {time.perf_counter() - started:.1f} s.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a deployed NearShop environment.")
    parser.add_argument("base_url", help="the environment's Vercel URL, for example https://nearshop-umber.vercel.app")
    parser.add_argument("--mode", choices=("read-only", "full"), default="read-only")
    parser.add_argument("--timeout", type=float, default=20, help="seconds allowed for each request (default 20)")
    parser.add_argument("--expect-commit", help="first wait until /api/health reports this git commit")
    parser.add_argument("--wait-seconds", type=float, default=600, help="how long to wait for it (default 600)")
    parser.add_argument("--poll-seconds", type=float, default=15, help="pause between health checks (default 15)")
    args = parser.parse_args(argv)
    if urllib.parse.urlsplit(args.base_url).scheme not in ("http", "https"):
        parser.error("base_url must start with https:// (or http:// for a local server)")
    bypass = os.environ.get("NEARSHOP_SMOKE_VERCEL_BYPASS")
    client = Client(args.base_url, args.timeout,
                    {"x-vercel-protection-bypass": bypass} if bypass else None)
    return Smoke(client, args.mode, expect_commit=args.expect_commit, wait_seconds=args.wait_seconds,
                 poll_seconds=args.poll_seconds, email=os.environ.get("NEARSHOP_SMOKE_EMAIL"),
                 password=os.environ.get("NEARSHOP_SMOKE_PASSWORD")).run()


if __name__ == "__main__":
    sys.exit(main())
