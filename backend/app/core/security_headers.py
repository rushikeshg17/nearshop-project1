"""Security headers on every API response (P2.6, plan section 8.13; docs/security/headers.md).

Every response the API sends, its errors, its 403 and 429 answers, an unhandled exception's 500 and /media
files included, carries:

- X-Content-Type-Options: nosniff, so a browser never guesses a JSON body or a photo to be HTML or script;
- Content-Security-Policy: default-src 'none'; frame-ancestors 'none'. An API response is never a page: if
  one is ever opened as a document, it may load nothing, run nothing and be framed by nobody;
- Referrer-Policy: no-referrer.

Cache-Control: private, no-store goes on every response to a request that carries a session (the session
cookie or a bearer token, whether or not it is still valid) and on every response under the private
prefixes below (the auth, account, owner and admin routes), signed in or not, replacing any Cache-Control
a route set. Neither the browser nor any cache between it and the API (Vercel, a proxy) may keep names,
phone numbers or addresses. One exception: /media files are the same public photo for everyone, so a
signed-in visitor's photo requests stay cacheable; nothing personal is ever served under /media.

In development only, FastAPI's interactive docs (/docs, /redoc) are left without the CSP, which would block
the scripts they load; they keep the other headers.
"""
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

API_CSP = "default-src 'none'; frame-ancestors 'none'"
NO_STORE = "private, no-store"
API_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": API_CSP,
    "Referrer-Policy": "no-referrer",
}

# The auth, account, owner and admin routes: every response under these is no-store, signed in or not. The
# account routes are the customer's own (app/api/routes/customer.py); /api/ai is the admin's model console.
PRIVATE_PREFIXES: tuple[str, ...] = (
    "/api/auth", "/api/me", "/api/reservations", "/api/orders", "/api/reviews", "/api/notifications",
    "/api/owner", "/api/admin", "/api/ai",
)
PUBLIC_FILE_PREFIX = "/media"


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def is_private_path(path: str) -> bool:
    """Whether `path` is an auth, account, owner or admin route."""
    return any(_under(path, prefix) for prefix in PRIVATE_PREFIXES)


def carries_session(headers: list[tuple[bytes, bytes]], cookie_name: str) -> bool:
    """Whether the request has a session: a non-empty session cookie or a bearer token. Not whether that
    session is still valid: a stale cookie is still a signed-in browser's request, and checking would cost a
    database read on every response."""
    for name, value in headers:
        if name == b"authorization":
            auth = value.decode("latin-1").strip()
            if auth[:7].lower() == "bearer " and auth[7:].strip():
                return True
        elif name == b"cookie" and cookie_parser(value.decode("latin-1")).get(cookie_name):
            return True
    return False


class SecurityHeadersMiddleware:
    """Adds API_HEADERS to every HTTP response, and no-store where it belongs (see the module docstring)."""

    def __init__(self, app: ASGIApp, docs_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.docs_paths = docs_paths  # left without the CSP (development only; see install)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        no_store = is_private_path(path) or (
            not _under(path, PUBLIC_FILE_PREFIX)
            and carries_session(scope.get("headers") or [], settings.cookie_name))
        skip_csp = path in self.docs_paths

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for name, value in API_HEADERS.items():
                    if not (skip_csp and name == "Content-Security-Policy"):
                        out[name] = value
                if no_store:
                    out["Cache-Control"] = NO_STORE
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install(app: Any) -> None:
    """Wrap the app's whole middleware stack, Starlette's error handling included, so every response gets the
    headers: the 500 of an unhandled exception and the edge and rate-limit answers too."""
    docs_paths: frozenset[str] = frozenset()
    if settings.environment == "development":
        docs_paths = frozenset(p for p in (app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url) if p)
    build_stack = app.build_middleware_stack

    def build_secured_stack() -> ASGIApp:
        return SecurityHeadersMiddleware(build_stack(), docs_paths)

    app.build_middleware_stack = build_secured_stack
