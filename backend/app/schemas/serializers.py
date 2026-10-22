"""Model -> JSON dict helpers. References (`product.category`, `reservation.shop`, ...) must be
resolved first with app/services/loaders.py. Timestamps are emitted as ISO-8601 UTC with a trailing Z so
browsers never mistake them for local time (important for 'updated 2 minutes ago')."""
from datetime import UTC, datetime, time, timedelta

from app.core.config import settings
from app.core.database import utcnow
from app.models import Category, Notification, Order, Product, Reservation, Review, Shop, StatusEvent
from app.services.media import thumbnail_key
from app.utils.hours import IST, open_status


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


# Stock freshness (P7.4): how long ago a listing's stock was confirmed. Up to FRESH_HOURS it is fresh (no label,
# no change). Beyond that and up to STALE_DAYS it is labelled "updated N days ago" and its search group ranks with
# FRESHNESS_MULTIPLIERS["aging"]; beyond STALE_DAYS it is labelled "stock not confirmed recently", ranks with
# FRESHNESS_MULTIPLIERS["stale"] and is left out of the in-stock-only filter (app/services/search.py). A listing a
# customer reported missing on arrival (Product.stock_unconfirmed_at, P9.1, plan section 8.17) is treated the same as
# one beyond STALE_DAYS until the shop next counts it, whenever its stock_updated_at was (confirmed_at).
FRESH_HOURS = 48
STALE_DAYS = 7
FRESHNESS_MULTIPLIERS = {"fresh": 1.0, "aging": 0.85, "stale": 0.6}
STALE_LABEL = "stock not confirmed recently"


def confirmed_at(stock_updated_at: datetime | None, stock_unconfirmed_at: datetime | None) -> datetime | None:
    """When a listing's stock was last confirmed, for its freshness band: None while an arrival report has unconfirmed
    it (P9.1), which bands it as stale."""
    return None if stock_unconfirmed_at is not None else stock_updated_at


def call_only_since() -> datetime:
    """When the call-only rule starts (P9.8): the start of the IST day settings.call_only_from, as naive UTC."""
    start = datetime.combine(settings.call_only_from, time(0), tzinfo=IST)
    return start.astimezone(UTC).replace(tzinfo=None)


def is_call_only(stock_updated_at: datetime | None, stock_unconfirmed_at: datetime | None,
                 now: datetime | None = None) -> bool:
    """Whether a listing is call-only at `now` (P9.8, plan section 8.17): from settings.call_only_from, its stock count
    is more than settings.call_only_after_days days old (or it has none), or an arrival failure has left it unconfirmed
    for longer than that. Customers are asked to call the shop, and reservations and delivery orders are refused. The
    shop's next stock update sets stock_updated_at and clears stock_unconfirmed_at, so the listing is restored at once."""
    now = now or utcnow()
    if now < call_only_since():
        return False
    limit = timedelta(days=settings.call_only_after_days)
    if stock_unconfirmed_at is not None and now - stock_unconfirmed_at > limit:
        return True
    return stock_updated_at is None or now - stock_updated_at > limit


def freshness(updated_at: datetime | None, now: datetime) -> dict:
    """A listing's freshness band at `now`: band (fresh, aging or stale), label (None when fresh) and the ranking
    multiplier. A listing with no confirmation time counts as stale."""
    age = now - updated_at if updated_at else None
    if age is not None and age <= timedelta(hours=FRESH_HOURS):
        band, label = "fresh", None
    elif age is not None and age <= timedelta(days=STALE_DAYS):
        days = age.days
        band, label = "aging", f"updated {days} day{'' if days == 1 else 's'} ago"
    else:
        band, label = "stale", STALE_LABEL
    return {"band": band, "label": label, "multiplier": FRESHNESS_MULTIPLIERS[band]}


def media_url(path: str | None) -> str | None:
    """The browser URL of a stored image key: on the bucket's public domain when one is set (staging and
    production), otherwise /media, which the API serves from local disk in development and test."""
    if not path:
        return None
    return f"{settings.media_public_base_url or '/media'}/{path}"


def thumbnail_url(path: str | None) -> str | None:
    """The browser URL of a stored image's 320 px thumbnail (app/services/media.py, thumbnail_key), for lists
    and small previews. Every image_url in these serializers has a thumbnail_url beside it. A photo stored
    before thumbnails existed has one once database/admin_tasks.py backfill-thumbnails has run."""
    return media_url(thumbnail_key(path)) if path else None


def category_out(c: Category) -> dict:
    return {"id": c.id, "slug": c.slug, "name": c.name, "icon": c.icon, "color_hue": c.color_hue,
            "description": c.description}


def shop_brief(s: Shop, distance_km: float | None = None) -> dict:
    hours = open_status(s.opening_hours, s.closed_on)
    return {
        "id": s.id,
        "slug": s.slug,
        "name": s.name,
        "tagline": s.tagline,
        "locality": s.locality,
        "city": s.city,
        "lat": s.lat,
        "lng": s.lng,
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "is_open": hours["is_open"],
        "hours_label": hours["label"],
        "offers_pickup": s.offers_pickup,
        "offers_delivery": s.offers_delivery,
        "delivery_radius_km": s.delivery_radius_km,
        "delivery_fee": s.delivery_fee,
        "free_delivery_above": s.free_delivery_above,
        "is_verified": s.is_verified,
        "image_url": media_url(s.image_path),
        "thumbnail_url": thumbnail_url(s.image_path),
        "inventory_updated_at": iso(s.inventory_updated_at),
        "rating_avg": s.rating_avg,
        "rating_count": s.rating_count,
        "categories": [c.slug for c in s.categories],
    }


def shop_detail(s: Shop, distance_km: float | None, reliability: dict) -> dict:
    rel = dict(reliability)
    rel["inventory_updated_at"] = iso(rel["inventory_updated_at"])
    return {
        **shop_brief(s, distance_km),
        "description": s.description,
        "phone": s.phone,
        "address_line": s.address_line,
        "pincode": s.pincode,
        "opening_hours": s.opening_hours,
        "closed_on": s.closed_on,
        "established_year": s.established_year,
        "hold_minutes": s.hold_minutes,
        "show_exact_stock": s.show_exact_stock,
        "is_active": s.is_active,
        "reliability": rel,
        "category_details": [category_out(c) for c in s.categories],
    }


def listing(p: Product, shop: dict | None = None, now: datetime | None = None) -> dict:
    """A product as sold by one shop (an 'offer'). `now` is the clock its freshness is judged at (search passes
    its own, so the evaluation harness can fix it). call_only (P9.8) is judged at the same clock."""
    now = now or utcnow()
    fresh = freshness(confirmed_at(p.stock_updated_at, p.stock_unconfirmed_at), now)
    return {
        "id": p.id,
        "catalog_item_id": p.catalog_item_id,
        "name": p.name,
        "brand": p.brand,
        "unit": p.unit,
        "icon": p.icon,
        "image_url": media_url(p.image_path),
        "thumbnail_url": thumbnail_url(p.image_path),
        "category": p.category.slug if p.category else None,
        "price": p.price,
        "mrp": p.mrp,
        "quantity": p.quantity,
        "low_stock_threshold": p.low_stock_threshold,
        "stock_status": p.stock_status,
        "stock_updated_at": iso(p.stock_updated_at),
        "freshness": {"band": fresh["band"], "label": fresh["label"]},
        "call_only": is_call_only(p.stock_updated_at, p.stock_unconfirmed_at, now),
        "is_active": p.is_active,
        "shop": shop,
    }


def shows_exact_stock(seller: Shop | None) -> bool:
    """Whether customers may see this shop's exact stock (P10.1, plan section 8.20): only when the shop opted in. An
    unknown shop counts as not opted in, so a caller that forgot to load it shows the capped count, never the exact one."""
    return bool(seller is not None and seller.show_exact_stock)


def public_quantity(quantity: int, exact: bool) -> int:
    """The stock count customers see: the exact count for an opted-in shop, otherwise at most settings.public_stock_cap."""
    return quantity if exact else min(quantity, settings.public_stock_cap)


def public_listing(p: Product, shop: dict | None = None, now: datetime | None = None, *,
                   seller: Shop | None = None) -> dict:
    """A listing as customers and anyone else outside the shop see it (P10.1, plan section 8.20). `seller` is the
    listing's Shop (p.shop when omitted). Without the shop's opt-in, quantity is capped at settings.public_stock_cap and
    stock_exact is false; stock_status and stock_updated_at come from the real count, unchanged. Every public caller
    uses this; owner, admin and analytics responses use listing, which keeps exact counts. A call-only listing (P9.8)
    also carries call_phone, the shop's phone for its "Call the shop to check" link (None otherwise)."""
    seller = seller if seller is not None else p.shop
    exact = shows_exact_stock(seller)
    out = listing(p, shop, now)
    out["quantity"] = public_quantity(p.quantity, exact)
    out["stock_exact"] = exact
    out["call_phone"] = (seller.phone or None) if out["call_only"] and seller is not None else None
    return out


# The close_reason an owner's "Out of stock after confirming" cancellation records (P9.8, plan section 8.17): an
# arrival failure, handled in app/services/reservations.py and app/services/orders.py. It is a code, so customers'
# responses leave it out (customer_close_reason) and their screens show the plain cancelled text instead.
OUT_OF_STOCK_AFTER_CONFIRM = "out_of_stock_after_confirm"


def customer_close_reason(close_reason: str | None, for_owner: bool) -> str | None:
    """The close_reason a response shows: as stored for the owner; for the customer, without the owner's out-of-stock
    code (P9.8)."""
    return None if close_reason == OUT_OF_STOCK_AFTER_CONFIRM and not for_owner else close_reason


def event_out(e: StatusEvent) -> dict:
    return {"from_status": e.from_status, "to_status": e.to_status, "actor_role": e.actor_role, "note": e.note,
            "at": iso(e.created_at)}


def person(u) -> dict:
    return {"id": u.id, "name": u.name, "phone": u.phone}


def reservation_out(r: Reservation, *, actions: list[str], events: list[StatusEvent] | None = None,
                    for_owner: bool = False, reviewed: bool = False) -> dict:
    p = r.product
    # The owner sees the exact count on the shelf; the customer the public count (P10.1).
    exact = for_owner or shows_exact_stock(r.shop)
    out = {
        "id": r.id,
        "code": r.code,
        "kind": "reservation",
        "status": r.status.value,
        "quantity": r.quantity,
        "unit_price": r.unit_price,
        "total": r.total,
        "note": r.note,
        "hold_minutes": r.hold_minutes,
        "expires_at": iso(r.expires_at),
        "created_at": iso(r.created_at),
        "confirmed_at": iso(r.confirmed_at),
        "ready_at": iso(r.ready_at),
        "completed_at": iso(r.completed_at),
        "closed_at": iso(r.closed_at),
        "close_reason": customer_close_reason(r.close_reason, for_owner),
        "product": {"id": p.id, "name": p.name, "brand": p.brand, "unit": p.unit, "icon": p.icon,
                    "image_url": media_url(p.image_path), "thumbnail_url": thumbnail_url(p.image_path),
                    "quantity_in_stock": public_quantity(p.quantity, exact), "stock_exact": exact},
        "shop": {"id": r.shop.id, "slug": r.shop.slug, "name": r.shop.name, "locality": r.shop.locality,
                 "address_line": r.shop.address_line, "phone": r.shop.phone, "lat": r.shop.lat, "lng": r.shop.lng,
                 "hours_label": open_status(r.shop.opening_hours, r.shop.closed_on)["label"]},
        "actions": actions,
        "reviewed": reviewed,
    }
    if for_owner:
        out["customer"] = person(r.customer)
    if events is not None:
        out["timeline"] = [event_out(e) for e in events]
    return out


def order_out(o: Order, *, actions: list[str], events: list[StatusEvent] | None = None,
              for_owner: bool = False, reviewed: bool = False) -> dict:
    out = {
        "id": o.id,
        "code": o.code,
        "kind": "order",
        "status": o.status.value,
        "payment_method": o.payment_method,
        "payment_status": o.payment_status,
        "delivery_address": o.delivery_address,
        "delivery_lat": o.delivery_lat,
        "delivery_lng": o.delivery_lng,
        "contact_phone": o.contact_phone if for_owner else None,
        "distance_km": o.distance_km,
        "subtotal": o.subtotal,
        "delivery_fee": o.delivery_fee,
        "total": o.total,
        "note": o.note,
        "created_at": iso(o.created_at),
        "delivered_at": iso(o.delivered_at),
        "closed_at": iso(o.closed_at),
        "close_reason": customer_close_reason(o.close_reason, for_owner),
        "items": [
            {"product_id": i.product_id, "name": i.name, "unit_price": i.unit_price, "quantity": i.quantity,
             "icon": i.product.icon if i.product else "Package",
             "image_url": media_url(i.product.image_path) if i.product else None,
             "thumbnail_url": thumbnail_url(i.product.image_path) if i.product else None}
            for i in o.items
        ],
        "shop": {"id": o.shop.id, "slug": o.shop.slug, "name": o.shop.name, "locality": o.shop.locality,
                 "phone": o.shop.phone, "lat": o.shop.lat, "lng": o.shop.lng},
        "actions": actions,
        "reviewed": reviewed,
    }
    if for_owner:
        out["customer"] = person(o.customer)
    if events is not None:
        out["timeline"] = [event_out(e) for e in events]
    return out


def review_out(r: Review, product_name: str | None = None, *, for_owner: bool = False) -> dict:
    """A review as lists show it, with the shop's reply under it (P3.6). Public lists show only visible reviews
    and a reply only while it is visible; the owner's list also carries each one's moderation status."""
    if r.customer is None or r.customer.deleted_at is not None:
        display = "Former customer"  # the reviewer deleted their account (P3.2)
    else:
        parts = (r.customer.name or "Customer").split()
        display = parts[0] + (f" {parts[-1][0]}." if len(parts) > 1 else "")
    return {
        "id": r.id,
        "rating": r.rating,
        "accuracy_rating": r.accuracy_rating,
        "delivery_rating": r.delivery_rating,
        "comment": r.comment,
        "created_at": iso(r.created_at),
        "customer_name": display,
        "product_name": product_name,
        "source": "delivery" if r.order_id else "pickup",
        "reply": reply_out(r, for_owner=for_owner),
        **({"status": r.status} if for_owner else {}),
    }


def reply_out(r: Review, *, for_owner: bool = False) -> dict | None:
    rp = r.reply
    if rp is None or (rp.status != "visible" and not for_owner):
        return None
    out = {"text": rp.text, "created_at": iso(rp.created_at), "edited_at": iso(rp.edited_at)}
    if for_owner:
        # The edit window (24 hours, app/services/reviews.py) and whether moderation has the reply.
        out.update(status=rp.status, under_review=rp.reported_at is not None and rp.decided_at is None)
    return out


def notification_out(n: Notification) -> dict:
    return {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "link": n.link,
            "read": n.read_at is not None, "created_at": iso(n.created_at)}
