"""Security headers on API responses and the secure-cookie startup check (P2.6, plan section 8.13)."""
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app import main
from app.api.routes import admin, ai, auth, customer, owner
from app.core.config import settings
from app.core.security_headers import (
    API_CSP,
    NO_STORE,
    SecurityHeadersMiddleware,
    carries_session,
    is_private_path,
)
from tests.conftest import HEADERS

BACKEND_DIR = Path(__file__).resolve().parents[1]
EXPECTED = {"x-content-type-options": "nosniff", "content-security-policy": API_CSP, "referrer-policy": "no-referrer"}


def assert_api_headers(response) -> None:
    for name, value in EXPECTED.items():
        assert response.headers.get(name) == value, (name, response.headers.get(name))


@pytest.fixture
def boom_route():
    def boom():
        raise RuntimeError("security headers test failure")

    main.app.add_api_route("/api/test-security-boom", boom, methods=["GET"])
    yield
    main.app.router.routes[:] = [r for r in main.app.router.routes
                                 if getattr(r, "path", None) != "/api/test-security-boom"]


# ---------------------------------------------------------------- the headers on every response


@pytest.mark.parametrize("path", ["/api/health", "/api/shops", "/api/search?q=charger", "/api/meta"])
def test_a_public_route_carries_the_api_headers_and_stays_cacheable_signed_out(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert_api_headers(response)
    assert "cache-control" not in response.headers


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/reservations", "/api/owner/overview", "/api/admin/overview"])
def test_a_private_route_carries_the_api_headers_and_no_store_signed_out(client, path):
    response = client.get(path)
    assert response.status_code in (200, 401, 403)
    assert_api_headers(response)
    assert response.headers["cache-control"] == NO_STORE


def test_errors_carry_the_headers_too(client, boom_route):
    for response in (client.get("/api/products/999999"),  # 404 from the route
                     client.get("/api/no-such-route"),  # 404 from the router
                     client.post("/api/auth/logout")):  # 403: no client header (csrf_guard)
        assert response.status_code in (403, 404)
        assert_api_headers(response)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        response = c.get("/api/test-security-boom")  # an unhandled exception's 500
    assert response.status_code == 500
    assert_api_headers(response)


def test_media_files_carry_the_headers(client):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    (settings.media_dir / "security-headers-test.txt").write_text("photo")
    response = client.get("/media/security-headers-test.txt")
    assert response.status_code == 200
    assert_api_headers(response)


# ---------------------------------------------------------------- no-store for signed-in requests


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/shops", "/api/search?q=charger", "/api/meta",
                                  "/api/me/dashboard", "/api/reservations"])
def test_responses_to_signed_in_requests_carry_no_store(customer, path):
    response = customer.get(path)
    assert response.status_code == 200
    assert_api_headers(response)
    assert response.headers["cache-control"] == NO_STORE


def test_a_bearer_token_counts_as_a_session(customer, client):
    token = customer.cookies.get(settings.cookie_name)
    with TestClient(main.app) as c:
        response = c.get("/api/shops", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_STORE


def test_signed_in_owner_and_admin_responses_carry_no_store(owner_a, admin):
    for c, path in ((owner_a, "/api/owner/overview"), (admin, "/api/admin/overview"), (owner_a, "/api/shops")):
        response = c.get(path, headers=HEADERS)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == NO_STORE


def test_no_store_replaces_a_cache_control_the_route_set(customer):
    def cached():
        return PlainTextResponse("ok", headers={"Cache-Control": "public, max-age=600"})

    main.app.add_api_route("/api/test-security-cached", cached, methods=["GET"])
    try:
        assert customer.get("/api/test-security-cached").headers["cache-control"] == NO_STORE
        with TestClient(main.app) as anonymous:
            assert anonymous.get("/api/test-security-cached").headers["cache-control"] == "public, max-age=600"
    finally:
        main.app.router.routes[:] = [r for r in main.app.router.routes
                                     if getattr(r, "path", None) != "/api/test-security-cached"]


def test_a_signed_in_visitors_photos_stay_cacheable(customer):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    (settings.media_dir / "security-headers-test.txt").write_text("photo")
    response = customer.get("/media/security-headers-test.txt")
    assert response.status_code == 200
    assert_api_headers(response)
    assert "cache-control" not in response.headers


def test_every_auth_account_owner_and_admin_route_is_private():
    for module in (auth, customer, owner, admin, ai):
        for route in module.router.routes:
            assert is_private_path("/api" + route.path), route.path
    for path in ("/api/health", "/api/shops", "/api/shops/x/products", "/api/products/1", "/api/search",
                 "/api/meta", "/api/administrator", "/media/x.webp"):
        assert not is_private_path(path), path


@pytest.mark.parametrize("headers, expected", [
    ([], False),
    ([(b"cookie", f"{settings.cookie_name}=abc".encode())], True),
    ([(b"cookie", f"theme=dark; {settings.cookie_name}=abc".encode())], True),
    ([(b"cookie", f"{settings.cookie_name}=".encode())], False),
    ([(b"cookie", b"theme=dark")], False),
    ([(b"authorization", b"Bearer abc")], True),
    ([(b"authorization", b"bearer abc")], True),
    ([(b"authorization", b"Bearer ")], False),
    ([(b"authorization", b"Basic abc")], False),
])
def test_carries_session(headers, expected):
    assert carries_session(headers, settings.cookie_name) is expected


def test_the_docs_paths_skip_only_the_csp():
    async def page(_):
        return PlainTextResponse("docs")

    app = SecurityHeadersMiddleware(Starlette(routes=[Route("/docs", page), Route("/other", page)]),
                                    frozenset({"/docs"}))
    with TestClient(app) as c:
        docs, other = c.get("/docs"), c.get("/other")
    assert "content-security-policy" not in docs.headers
    assert docs.headers["x-content-type-options"] == "nosniff"
    assert docs.headers["referrer-policy"] == "no-referrer"
    assert_api_headers(other)


def test_the_test_app_keeps_the_csp_on_its_docs(client):
    assert_api_headers(client.get("/docs"))  # docs are exempt in development only


# ---------------------------------------------------------------- secure cookies at startup


@pytest.mark.parametrize("environment", ["staging", "production", "demo"])
def test_insecure_cookies_are_refused_in_deployed_environments(environment):
    problem = settings.model_copy(update={"environment": environment, "cookie_secure": False}).insecure_cookie_problem()
    assert problem and "NEARSHOP_COOKIE_SECURE=true" in problem and environment in problem
    assert settings.model_copy(update={"environment": environment, "cookie_secure": True}).insecure_cookie_problem() is None


@pytest.mark.parametrize("environment", ["development", "test"])
def test_insecure_cookies_are_allowed_locally(environment):
    assert settings.model_copy(update={"environment": environment, "cookie_secure": False}).insecure_cookie_problem() is None


def test_startup_in_production_with_insecure_cookies_exits_non_zero(tmp_path):
    """The real start command (uvicorn) exits before serving: the lifespan refuses to start. Nothing connects to
    a database: the refusal comes before the scheduler and the startup checks."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEARSHOP_")}
    env.update(
        NEARSHOP_ENVIRONMENT="production", NEARSHOP_COOKIE_SECURE="false",
        # Everything backend/.env could otherwise supply is set here, so nothing of it is used.
        NEARSHOP_MONGODB_URI="mongodb://127.0.0.1:1/?directConnection=true&serverSelectionTimeoutMS=500",
        NEARSHOP_MONGODB_DB="nearshop_test_unused", NEARSHOP_PROTECTED_HOSTS="",
        NEARSHOP_SECRET_KEY=secrets.token_urlsafe(32), NEARSHOP_EDGE_SECRET=secrets.token_urlsafe(32),
        NEARSHOP_ENABLE_SCHEDULER="false", NEARSHOP_WARM_ON_STARTUP="false", NEARSHOP_SENTRY_DSN="",
        NEARSHOP_MEDIA_BACKEND="local", NEARSHOP_MEDIA_DIR=str(tmp_path / "media"),
        NEARSHOP_MEDIA_PUBLIC_BASE_URL="", NEARSHOP_MODEL_DIR=str(tmp_path / "models"),
    )
    result = subprocess.run([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
                            cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr  # the API logs JSON lines on stdout
    assert result.returncode != 0
    assert "NEARSHOP_COOKIE_SECURE=true" in output
    assert "Application startup failed" in output
