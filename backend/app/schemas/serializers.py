"""Model -> JSON dict helpers. References (`product.category`, `reservation.shop`, ...) must be
resolved first with app/services/loaders.py. Timestamps are emitted as ISO-8601 UTC with a trailing Z so
browsers never mistake them for local time (important for 'updated 2 minutes ago')."""
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.database import utcnow
from app.models import Category, Notification, Order, Product, Reservation, Review, Shop, StatusEvent
from app.services.media import thumbnail_key
from app.utils.hours import open_status


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


# Stock freshness (P7.4): how long ago a listing's stock was confirmed. Up to FRESH_HOURS it is fresh (no label,
# no change). Beyond that and up to STALE_DAYS it is labelled "updated N days ago" and its search group ranks with
# FRESHNESS_MULTIPLIERS["aging"]; beyond STALE_DAYS it is labelled "stock not confirmed recently", ranks with
# FRESHNESS_MULTIPLIERS["stale"] and is left out of the in-stock-only filter (app/services/search.py).
FRESH_HOURS = 48
STALE_DAYS = 7
FRESHNESS_MULTIPLIERS = {"fresh": 1.0, "aging": 0.85, "stale": 0.6}
STALE_LABEL = "stock not confirmed recently"


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
        "is_active": s.is_active,
        "reliability": rel,
        "category_details": [category_out(c) for c in s.categories],
    }


def listing(p: Product, shop: dict | None = None, now: datetime | None = None) -> dict:
    """A product as sold by one shop (an 'offer'). `now` is the clock its freshness is judged at (search passes
    its own, so the evaluation harness can fix it)."""
    fresh = freshness(p.stock_updated_at, now or utcnow())
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
        "is_active": p.is_active,
        "shop": shop,
    }


def event_out(e: StatusEvent) -> dict:
    return {"from_status": e.from_status, "to_status": e.to_status, "actor_role": e.actor_role, "note": e.note,
            "at": iso(e.created_at)}


def person(u) -> dict:
    return {"id": u.id, "name": u.name, "phone": u.phone}


def reservation_out(r: Reservation, *, actions: list[str], events: list[StatusEvent] | None = None,
                    for_owner: bool = False, reviewed: bool = False) -> dict:
    p = r.product
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
        "close_reason": r.close_reason,
        "product": {"id": p.id, "name": p.name, "brand": p.brand, "unit": p.unit, "icon": p.icon,
                    "image_url": media_url(p.image_path), "thumbnail_url": thumbnail_url(p.image_path),
                    "quantity_in_stock": p.quantity},
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
        "close_reason": o.close_reason,
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
