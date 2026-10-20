"""FastAPI dependencies: database handle, client address, current user, role guards, CSRF check."""
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.database import Database, get_db
from app.core.security import decode_access_token
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


def get_optional_user(request: Request, db: DB) -> User | None:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    user = db.users.get(int(payload["sub"]))
    if not user or not user.is_active:
        return None
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
