from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.deps import DB, AdminUser, OptionalUser
from app.services import search as search_service
from app.services import search_events
from app.services.meta import city_config

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
def search(
    request: Request,
    response: Response,
    db: DB,
    user: OptionalUser,
    q: str = Query("", max_length=120),
    lat: float | None = Query(None, ge=-90, le=90),
    lng: float | None = Query(None, ge=-180, le=180),
    # Up to 50 km is still accepted (links and clients from before P8.2); run_search searches at most
    # MAX_RADIUS_KM, 25 km, and the response's radius_km is the radius searched. The requested radius stays in
    # the search's filters (P5.6 interaction logging).
    radius_km: float = Query(5, gt=0, le=50),
    category: str | None = None,
    min_price: float | None = Query(None, ge=0),
    max_price: float | None = Query(None, ge=0),
    in_stock_only: bool = False,
    fulfillment: Literal["pickup", "delivery"] | None = None,
    sort: Literal["relevance", "distance", "price_asc", "price_desc"] = "relevance",
    page: int = Query(1, ge=1, le=50),
    page_size: int = Query(20, ge=1, le=search_service.MAX_PAGE_SIZE),
):
    if lat is None or lng is None:
        center = city_config()["center"]
        lat, lng = center["lat"], center["lng"]
    params = search_service.SearchParams(
        q=q, lat=lat, lng=lng, radius_km=radius_km, category=category, min_price=min_price, max_price=max_price,
        in_stock_only=in_stock_only, fulfillment=fulfillment, sort=sort, page=page, page_size=page_size,
    )
    user_id = user.id if user else None
    # Interaction logging (P5.6, app/services/search_events.py): only for a signed-in customer with the
    # search-measurement consent. Anyone else's trail cookie is deleted, and their search records nothing.
    cookie = request.cookies.get(search_events.TRAIL_COOKIE)
    plan = None
    try:
        measured = search_events.measured(db, user)
        if not measured and cookie is not None:
            search_events.clear_trail(response)
        plan = search_events.plan(params, cookie) if measured else None
    except Exception as exc:  # logging never fails a search
        search_events.log.warning("search not measured: %s", type(exc).__name__)
    if plan is None:
        return search_service.run_search(db, params, user_id=user_id)
    result = search_service.run_search(db, params, user_id=user_id, cache=plan.cache, now=plan.now)
    try:
        search_events.record_search(db, plan, params, result, response)
    except Exception as exc:
        search_events.log.warning("search event not recorded: %s", type(exc).__name__)
        return result
    return {**result, "search_id": plan.search_id}


class ClickIn(BaseModel):
    """A search result the customer opened (the result card sends it; P5.6)."""

    model_config = ConfigDict(extra="ignore")
    listing_id: int = Field(ge=1)
    group: str = Field(pattern=r"^[cp][0-9]{1,18}$")
    position: int = Field(ge=1, le=1000)


async def click_body(request: Request) -> ClickIn | None:
    """The click's body, or None when it is not valid JSON or not a ClickIn: the click endpoint answers 204 to any
    body once past the CSRF guard and the rate limit, so it never raises a validation error."""
    try:
        return ClickIn.model_validate_json(await request.body())
    except (ValidationError, ValueError):
        return None


@router.post("/click", status_code=204)
def click(request: Request, response: Response, db: DB, user: OptionalUser,
          body: Annotated[ClickIn | None, Depends(click_body)]) -> Response:
    """Record a click on a search result. 204 whether or not anything is recorded: nothing for a body that is not
    a click, without a trail, or for a caller who is not measured. Rate limit: 120 per IP per minute
    (app/core/ratelimit.py)."""
    try:
        if body is not None:
            search_events.record_click(db, request, response, user, body.listing_id, body.group, body.position)
    except Exception as exc:  # a click is never worth an error
        search_events.log.warning("search click not recorded: %s", type(exc).__name__)
    response.status_code = 204
    return response


@router.get("/online")
def online(user: AdminUser, db: DB):
    """Admin only (P5.6): the online search metrics over 7 and 30 days, overall and per language, the shadow
    comparison per candidate label with the plan section 8.6 guardrails, and the latest saved snapshots."""
    return {**search_events.online_report(db), "snapshots": search_events.snapshot_rows(db)}


@router.post("/online/snapshot")
def online_snapshot(user: AdminUser, db: DB):
    """Admin only (P5.6): save the online metrics and the shadow comparison as a model_runs entry named
    search_online, with the windows and the live ranking config version."""
    return search_events.save_snapshot(db)


@router.get("/suggest")
def suggest(db: DB, q: str = Query("", max_length=80)):
    return search_service.suggestions(db, q)


@router.get("/discover")
def discover(db: DB, user: OptionalUser):
    return {
        "popular": search_service.popular_searches(db),
        "recent": search_service.recent_searches(db, user.id) if user else [],
    }
