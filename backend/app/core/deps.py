"""FastAPI dependencies: database handle, client address, current user, role guards (the owner's shop, and the shop
whose daily work an owner or its active staff do), CSRF check."""
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.database import Database, get_db, utcnow
from app.core.security import LAST_SEEN_EVERY, decode_access_token
from app.models import Role, Shop, User
from app.services.loaders import with_categories

DB = Annotated[Database, Depends(get_db)]

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-nearshop-client"


def csrf_guard(request: Request) -> None:
    """Cookie auth + SameSite=Lax already blocks most CSRF. As defence in depth, every
    state-changing request must carry a custom header, which a cross-site form cannot set
    and cross-site fetch cannot send without passing our CORS allow-list."""
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "web":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing client header")


def get_client_ip(request: Request) -> str:
    """The visitor's address, as app/core/ratelimit.py found it: X-NearShop-Client-IP when the request carries
    the edge secret (through Vercel, or a server-rendered page's call), else the TCP peer. Never a header the
    visitor could set directly."""
    ip = getattr(request.state, "client_ip", None)
    return ip or (request.client.host if request.client else "unknown")


ClientIP = Annotated[str, Depends(get_client_ip)]


def session_token(request: Request) -> str | None:
    """The session token: the cookie, else a bearer token."""
    token = request.cookies.get(settings.cookie_name)
    if not token:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
    return token or None


def get_optional_user(request: Request, db: DB) -> User | None:
    """The signed-in user, or None. A token counts only while its user is active, its role and token_version
    match the user document and its session (sid) is still recorded there (P2.4): logout, sign-out-everywhere,
    a password or phone change, a role change and deactivation all end sessions at once. Everything is checked
    against the one user read this dependency always made; the only write is last_seen_at, at most once an
    hour per session. The session id is left on request.state.session_id for the session routes."""
    token = session_token(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or not str(payload.get("sub", "")).isdigit():
        return None
    user = db.users.get(int(payload["sub"]))
    if not user or not user.is_active:
        return None
    sid = payload.get("sid")
    if payload.get("ver") != user.token_version or payload.get("role") != user.role.value or not sid:
        return None
    session = next((s for s in user.sessions if s.id == sid), None)
    now = utcnow()
    if session is None or session.expires_at <= now:
        return None
    if now - session.last_seen_at >= LAST_SEEN_EVERY:
        db.users.update_one({"_id": user.id, "sessions.id": sid}, {"$set": {"sessions.$.last_seen_at": now}})
        session.last_seen_at = now
    request.state.session_id = sid
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def get_current_user(user: OptionalUser) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in to continue")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: Role):
    def checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this area")
        return user

    return checker


CustomerUser = Annotated[User, Depends(require_role(Role.CUSTOMER))]
OwnerUser = Annotated[User, Depends(require_role(Role.OWNER))]
AdminUser = Annotated[User, Depends(require_role(Role.ADMIN))]


def get_owner_shop(user: OwnerUser, db: DB) -> Shop:
    """The signed-in owner's shop. V1: one shop per owner account."""
    shop = db.shops.find_one({"owner_id": user.id}, sort=[("_id", 1)])
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You have not set up a shop yet")
    return with_categories(db, [shop])[0]


OwnerShop = Annotated[Shop, Depends(get_owner_shop)]


def get_work_shop(user: CurrentUser, db: DB) -> Shop:
    """The shop whose daily work the signed-in user may do (P9.13): the owner's own shop, or the shop of an active staff
    member. Daily work is the reservation and order queues and their actions, listings and stock, the catalogue and
    imports; everything else in backend/app/api/routes/owner.py stays owner-only (OwnerShop). A staff member's shop is
    first fitted to its plan's logins (app/services/staff.py reconcile), so one suspended by a lapse never gets in."""
    if user.role == Role.OWNER:
        return get_owner_shop(user, db)
    if user.role == Role.STAFF:
        from app.services import staff  # imported here: staff -> plans -> this module

        shop = staff.shop_for(db, user)
        if shop is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your staff access to this shop is paused or has ended. "
                                                           "Ask the shop's owner.")
        return with_categories(db, [shop])[0]
    raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this area")


WorkShop = Annotated[Shop, Depends(get_work_shop)]
