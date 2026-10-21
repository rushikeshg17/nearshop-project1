"""Shop owner: shop settings, inventory, catalog, reservation and delivery queues, insights."""
import re
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.ai.semantic_index import index
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.deps import DB, OwnerShop, OwnerUser
from app.core.errors import AppError, Conflict, NotFound
from app.models import Category, GeoPoint, InventoryReason, Order, Product, Reservation, Shop
from app.models.core import (
    RENEWAL_WINDOW_DAYS,
    KycType,
    VerificationStatus,
    evidence_keys,
    validate_kyc_number,
)
from app.schemas.requests import (
    CatalogAddIn,
    ProductIn,
    ProductUpdateIn,
    ShopUpdateIn,
    StockIn,
    TransitionIn,
)
from app.schemas.serializers import (
    iso,
    listing,
    media_url,
    order_out,
    reservation_out,
    review_out,
    shop_detail,
    thumbnail_url,
)
from app.services import analytics, importer
from app.services import orders as order_service
from app.services import reservations as reservation_service
from app.services.inventory import apply_bulk, change_stock, set_stock
from app.services.loaders import (
    categories,
    category_by_slug,
    hydrate_orders,
    hydrate_reservations,
    product_names,
    timelines,
    with_category,
    with_customers,
)
from app.services.media import delete_image, delete_private, save_image, save_private_image
from app.services.reliability import shop_reliability
from app.services.search import reset_vocabulary
from app.utils.text import normalize_query

router = APIRouter(prefix="/owner", tags=["shop owner"])

RES_ACTIVE = list(reservation_service.ACTIVE)
ORDER_OPEN = [*order_service.ACTIVE, "DELIVERY_FAILED"]


def _listings_changed() -> None:
    """Search caches (semantic index, spelling vocabulary) are rebuilt on the next query."""
    index.mark_dirty()
    reset_vocabulary()


# ------------------------------------------------------------------ verification (P3.3, plan section 8.1)

# A change to any of these sends a verified shop back to review (hidden until an admin approves it again).
REVIEW_FIELDS = ("address_line", "locality", "pincode", "location", "phone")


def verification_out(shop: Shop) -> dict:
    """The owner's view of their shop's verification. changes_need_review warns the settings screen (P3.4)
    before an address or phone change sends a verified shop back to review."""
    now = utcnow()
    status = shop.verification_status
    in_force = status == VerificationStatus.VERIFIED and shop.verified_until is not None and shop.verified_until > now
    renewal_opens_at = shop.verified_until - timedelta(days=RENEWAL_WINDOW_DAYS) if in_force else None
    return {
        "status": status.value,
        "visible_to_customers": shop.is_live,
        "kyc_type": shop.kyc_type.value if shop.kyc_type else None,
        "kyc_number": shop.kyc_number,
        "has_document_photo": bool(shop.kyc_document_key),
        "has_storefront_photo": bool(shop.kyc_storefront_key),
        "phone_verified": shop.phone_verified_at is not None,
        "submitted_at": iso(shop.submitted_at),
        "decided_at": iso(shop.decided_at),
        # Set by a rejection or suspension, and by a rejected renewal (the shop stays verified until
        # verified_until); cleared by the next submission or approval.
        "rejection_reason": shop.rejection_reason,
        "verified_until": iso(shop.verified_until),
        "renewal_pending": shop.renewal_pending,
        "renewal_opens_at": iso(renewal_opens_at),
        "can_submit": not (in_force and now < renewal_opens_at) and not shop.renewal_pending,
        "changes_need_review": status == VerificationStatus.VERIFIED,
    }


def _shop_settings(db: Database, shop: Shop, review_started: bool = False) -> dict:
    return {**shop_detail(shop, None, shop_reliability(db, shop)), "verification": verification_out(shop),
            "review_started": review_started}


@router.get("/verification")
def my_verification(shop: OwnerShop):
    return verification_out(shop)


@router.post("/verification")
def submit_verification(
    shop: OwnerShop, db: DB,
    kyc_type: KycType = Form(...),
    kyc_number: str = Form(..., max_length=60),
    storefront_photo: UploadFile = File(...),
    document_photo: UploadFile | None = File(None),
):
    """Submit KYC evidence: the number, a storefront photo showing the signboard and, for a trade licence, a photo
    of it. The status becomes submitted, so an admin reviews it; the shop stays hidden until approved. A verified
    shop may renew from RENEWAL_WINDOW_DAYS before its approval ends: it stays visible meanwhile and is listed as
    a renewal. Plain def: decoding and storage run in the thread pool (as the other uploads, P2.7)."""
    now = utcnow()
    renewal = False
    if shop.verification_status == VerificationStatus.VERIFIED and shop.verified_until and shop.verified_until > now:
        if shop.renewal_pending:
            raise Conflict("Your renewal is already waiting for review", code="renewal_pending")
        opens = shop.verified_until - timedelta(days=RENEWAL_WINDOW_DAYS)
        if now < opens:
            raise Conflict(f"Your shop is verified until {shop.verified_until:%d %b %Y}. You can renew from "
                           f"{opens:%d %b %Y}.", code="renewal_not_open")
        renewal = True
    try:
        number = validate_kyc_number(kyc_type, kyc_number)
    except ValueError as exc:
        raise AppError(str(exc), code="invalid_kyc_number", status_code=422) from None
    if kyc_type == KycType.TRADE_LICENCE and document_photo is None:
        raise AppError("Add a photo of your trade licence or Shop and Establishment registration",
                       code="document_required", status_code=422)

    document_key = save_private_image(document_photo, "documents") if document_photo is not None else None
    try:
        storefront_key = save_private_image(storefront_photo, "storefronts")
    except Exception:
        delete_private(document_key)
        raise
    fields: dict = {"kyc_type": kyc_type, "kyc_number": number, "kyc_document_key": document_key,
                    "kyc_storefront_key": storefront_key, "submitted_at": now, "rejection_reason": None}
    if renewal:
        # The evidence behind the approval still in force is kept until the renewal is decided: restored if
        # an admin rejects the renewal, deleted once it is approved (admin.py).
        fields["previous_kyc"] = {"kyc_type": shop.kyc_type.value if shop.kyc_type else None,
                                  "kyc_number": shop.kyc_number, "kyc_document_key": shop.kyc_document_key,
                                  "kyc_storefront_key": shop.kyc_storefront_key}
        stale: list[str | None] = []
    else:
        # Nothing approved rests on the evidence this replaces, so it is not kept.
        stale = [shop.kyc_document_key, shop.kyc_storefront_key, *evidence_keys(shop.previous_kyc)]
        fields.update(verification_status=VerificationStatus.SUBMITTED, is_verified=False, previous_kyc=None)
    db.shops.set(shop, **fields)
    for key in stale:
        delete_private(key)
    return verification_out(shop)


# ------------------------------------------------------------------ shop

@router.get("/shop")
def my_shop(shop: OwnerShop, db: DB):
    return _shop_settings(db, shop)


@router.patch("/shop")
def update_shop(body: ShopUpdateIn, shop: OwnerShop, db: DB):
    data = body.model_dump(exclude_unset=True)
    if "category_slugs" in data:
        slugs = set(data.pop("category_slugs"))
        cats = [c for c in categories(db).values() if c.slug in slugs]
        if not cats:
            raise AppError("Choose at least one category")
        data["category_ids"] = [c.id for c in cats]
        shop.categories = cats
    if "lat" in data or "lng" in data:
        data["location"] = GeoPoint.of(data.pop("lat", None) or shop.lat, data.pop("lng", None) or shop.lng)
    offers_delivery = data.get("offers_delivery", shop.offers_delivery)
    if offers_delivery and data.get("delivery_radius_km", shop.delivery_radius_km) <= 0:
        raise AppError("Set a delivery radius to offer delivery")
    # Re-verification (P3.3): a new phone is unverified, and a verified shop that changes its address or phone
    # goes back to review, hidden from customers until an admin approves it again.
    changed = {k for k in REVIEW_FIELDS if k in data and data[k] != getattr(shop, k)}
    if "phone" in changed:
        data["phone_verified_at"] = None
    review_started = bool(changed) and shop.verification_status == VerificationStatus.VERIFIED
    if review_started:
        data.update(verification_status=VerificationStatus.SUBMITTED, is_verified=False, submitted_at=utcnow())
    if data:
        db.shops.set(shop, **data)
    return _shop_settings(db, shop, review_started)


# The three upload routes are plain def handlers: FastAPI runs them in its thread pool, so decoding, parsing,
# storage and database work never blocks the event loop (P2.7). The body was already capped at
# settings.max_request_body_mb (app/main.py) and spooled by Starlette before the handler runs.
@router.post("/shop/image")
def upload_shop_image(shop: OwnerShop, db: DB, file: UploadFile = File(...)):
    rel = save_image(file, "shops")
    delete_image(shop.image_path)
    db.shops.set(shop, image_path=rel)
    return {"image_url": media_url(rel), "thumbnail_url": thumbnail_url(rel)}


@router.get("/overview")
def overview(shop: OwnerShop, db: DB):
    reservation_service.expire_due(db)
    data, requests, pending = db.gather(
        lambda: analytics.owner_overview(db, shop),
        lambda: db.reservations.find({"shop_id": shop.id, "status": "REQUESTED"}, sort=[("created_at", 1)], limit=6),
        lambda: db.orders.find({"shop_id": shop.id, "status": "PENDING"}, sort=[("created_at", 1)], limit=6),
    )
    data["reservation_requests"] = _res(db, requests)
    data["pending_orders"] = _ord(db, pending)
    data["shop"] = {"id": shop.id, "name": shop.name, "slug": shop.slug, "offers_delivery": shop.offers_delivery}
    return data


@router.get("/insights")
def insights(shop: OwnerShop, db: DB):
    return analytics.owner_insights(db, shop)


@router.get("/reviews")
def reviews(shop: OwnerShop, db: DB):
    rows = with_customers(db, db.reviews.find({"shop_id": shop.id}, sort=[("created_at", -1)], limit=50))
    names = product_names(db, (r.product_id for r in rows))
    return [review_out(r, names.get(r.product_id)) for r in rows]


# ------------------------------------------------------------------ inventory

def _own_product(db: Database, shop: Shop, product_id: int) -> Product:
    p = db.products.get(product_id)
    if p is None or p.shop_id != shop.id:
        raise NotFound("Product not found in your shop")
    return with_category(db, [p])[0]


def _keywords(tags: list[str], category: Category, subcategory: str | None = None) -> str:
    return " ".join(dict.fromkeys([*tags, category.name.lower(), (subcategory or "").replace("-", " ")])).strip()


@router.get("/products")
def list_products(
    shop: OwnerShop, db: DB,
    q: str = Query("", max_length=120),
    stock: Literal["all", "in_stock", "low_stock", "out_of_stock", "inactive"] = "all",
    category: str | None = None,
    sort: Literal["name", "stock_asc", "stock_desc", "price_asc", "price_desc", "updated"] = "name",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    query: dict = {"shop_id": shop.id, "is_active": stock != "inactive"}
    if stock == "in_stock":
        query["$expr"] = {"$gt": ["$quantity", "$low_stock_threshold"]}
    elif stock == "low_stock":
        query["quantity"] = {"$gt": 0}
        query["$expr"] = {"$lte": ["$quantity", "$low_stock_threshold"]}
    elif stock == "out_of_stock":
        query["quantity"] = 0
    if category:
        cat = category_by_slug(db, category)
        query["category_id"] = cat.id if cat else -1
    if q:
        like = {"$regex": re.escape(normalize_query(q)), "$options": "i"}
        query["$or"] = [{"name": like}, {"brand": like}, {"sku": like}]
    order = {
        "name": [("name", 1)], "stock_asc": [("quantity", 1)], "stock_desc": [("quantity", -1)],
        "price_asc": [("price", 1)], "price_desc": [("price", -1)], "updated": [("stock_updated_at", -1)],
    }[sort]
    total, rows = db.gather(
        lambda: db.products.count(query),
        lambda: with_category(db, db.products.find(query, sort=[*order, ("_id", 1)], skip=(page - 1) * page_size,
                                                   limit=page_size)),
    )
    items = [
        {**listing(p), "sku": p.sku, "description": p.description, "specs": p.specs or {}, "keywords": p.keywords}
        for p in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/products")
def create_product(body: ProductIn, shop: OwnerShop, user: OwnerUser, db: DB):
    cat = category_by_slug(db, body.category_slug)
    if cat is None:
        raise AppError("Unknown category")
    p = Product(shop_id=shop.id, category_id=cat.id, name=body.name, brand=body.brand, sku=body.sku,
                unit=body.unit, description=body.description, specs=body.specs,
                keywords=_keywords([t.lower() for t in body.tags], cat), icon=cat.icon, price=body.price,
                mrp=body.mrp, quantity=0, low_stock_threshold=body.low_stock_threshold, category=cat)

    def create() -> None:
        db.products.insert(p)
        if body.quantity:
            change_stock(db, p, body.quantity, InventoryReason.INITIAL, user.id)

    db.transaction(create)
    _listings_changed()
    return listing(p)


@router.patch("/products/{product_id}")
def update_product(product_id: int, body: ProductUpdateIn, shop: OwnerShop, db: DB):
    p = _own_product(db, shop, product_id)
    data = body.model_dump(exclude_unset=True)
    if "category_slug" in data:
        cat = category_by_slug(db, data.pop("category_slug"))
        if cat is None:
            raise AppError("Unknown category")
        data["category_id"], p.category = cat.id, cat
    if "tags" in data:
        data["keywords"] = _keywords([t.lower() for t in data.pop("tags")], p.category)
    if data:
        db.products.set(p, **data)
    _listings_changed()
    return listing(p)


@router.put("/products/{product_id}/stock")
def update_stock(product_id: int, body: StockIn, shop: OwnerShop, user: OwnerUser, db: DB):
    p = _own_product(db, shop, product_id)
    set_stock(db, p, body.quantity, user.id)
    return listing(p)


@router.delete("/products/{product_id}")
def delete_product(product_id: int, shop: OwnerShop, db: DB):
    """Soft delete: the listing disappears from search but past orders and reviews keep their history."""
    p = _own_product(db, shop, product_id)
    active_holds = db.reservations.count({"product_id": p.id, "status": {"$in": RES_ACTIVE}})
    if active_holds:
        raise Conflict(f"This item has {active_holds} active reservation(s). Complete or cancel them first.")
    db.products.set(p, is_active=False)
    _listings_changed()
    return {"ok": True}


@router.post("/products/{product_id}/image")
def upload_product_image(product_id: int, shop: OwnerShop, db: DB, file: UploadFile = File(...)):
    p = _own_product(db, shop, product_id)
    rel = save_image(file, "products")
    delete_image(p.image_path)
    db.products.set(p, image_path=rel)
    return listing(p)


@router.get("/catalog")
def browse_catalog(shop: OwnerShop, db: DB, q: str = Query("", max_length=120), category: str | None = None,
                   limit: int = Query(60, ge=1, le=200)):
    query: dict = {}
    if category:
        cat = category_by_slug(db, category)
        query["category_id"] = cat.id if cat else -1
    elif not q:
        query["category_id"] = {"$in": shop.category_ids}
    if q:
        like = {"$regex": re.escape(normalize_query(q)), "$options": "i"}
        query["$or"] = [{"name": like}, {"brand": like}, {"subcategory": like}]
    found = with_category(db, db.catalog_items.find(query, sort=[("name", 1)], limit=limit))
    ids = [c.id for c in found]
    listed, medians = db.gather(
        lambda: {d["catalog_item_id"] for d in db.products.find_raw(
            {"shop_id": shop.id, "is_active": True, "catalog_item_id": {"$in": ids}}, {"catalog_item_id": 1})},
        # Local price guidance: median price of each item across shops, computed in the database.
        lambda: {r["_id"]: r["median"] for r in db.products.aggregate([
            {"$match": {"is_active": True, "catalog_item_id": {"$in": ids}}},
            {"$group": {"_id": "$catalog_item_id",
                        "median": {"$median": {"input": "$price", "method": "approximate"}}}},
        ])},
    )
    items = [
        {"id": c.id, "name": c.name, "brand": c.brand, "unit": c.unit, "icon": c.icon, "mrp": c.mrp,
         "typical_price": c.typical_price, "local_median": medians.get(c.id), "category": c.category.slug,
         "subcategory": c.subcategory, "already_listed": c.id in listed}
        for c in found
    ]
    return {"items": items}


@router.post("/catalog/add")
def add_from_catalog(body: CatalogAddIn, shop: OwnerShop, user: OwnerUser, db: DB):
    wanted = {i.catalog_item_id: i for i in body.items}
    catalog, existing = db.gather(
        lambda: db.catalog_items.by_ids(wanted),
        lambda: {p.catalog_item_id: p for p in db.products.find(
            {"shop_id": shop.id, "catalog_item_id": {"$in": list(wanted)}})},
    )
    if len(catalog) != len(wanted):
        raise NotFound("Catalog item not found")
    cats = categories(db)
    creates, updates = [], []
    for cid, item in wanted.items():
        c = catalog[cid]
        if cid in existing:
            if not existing[cid].is_active:  # previously removed: bring it back with the new price and count
                updates.append((existing[cid], {"is_active": True, "price": item.price}, item.quantity))
            continue
        creates.append((Product(shop_id=shop.id, catalog_item_id=c.id, category_id=c.category_id, name=c.name,
                                brand=c.brand, unit=c.unit, description=c.description, specs=c.specs or {},
                                keywords=_keywords(c.tags or [], cats[c.category_id], c.subcategory), icon=c.icon,
                                price=item.price, mrp=c.mrp, quantity=0), item.quantity))
    apply_bulk(db, shop, user.id, creates, updates)
    _listings_changed()
    return {"added": len(creates), "reactivated": len(updates)}


# ------------------------------------------------------------------ reservations & orders

class ShopTransitionIn(TransitionIn):
    """An owner's action on a reservation or order. code is required to complete a pickup (the reservation code
    the customer shows) and to deliver an order (the 4-digit delivery code only the customer sees), once
    settings.completion_codes_required is on (P3.5; P3.7 turns it on with the screens that ask for it)."""

    code: str | None = Field(default=None, max_length=20)


def _res(db: Database, rows: list[Reservation], with_events: bool = False) -> list[dict]:
    hydrate_reservations(db, rows, customers=True)
    events = timelines(db, "reservation", [r.id for r in rows]) if with_events else {}
    return [reservation_out(r, actions=reservation_service.allowed_actions(r, "owner"), for_owner=True,
                            events=events.get(r.id) if with_events else None) for r in rows]


def _ord(db: Database, rows: list[Order], with_events: bool = False) -> list[dict]:
    hydrate_orders(db, rows, customers=True)
    events = timelines(db, "order", [o.id for o in rows]) if with_events else {}
    return [order_out(o, actions=order_service.allowed_actions(o, "owner"), for_owner=True,
                      events=events.get(o.id) if with_events else None) for o in rows]


def _shop_reservation(db: Database, shop: Shop, reservation_id: int) -> Reservation:
    r = db.reservations.get(reservation_id)
    if r is None or r.shop_id != shop.id:
        raise NotFound("Reservation not found")
    return r


def _shop_order(db: Database, shop: Shop, order_id: int) -> Order:
    o = db.orders.get(order_id)
    if o is None or o.shop_id != shop.id:
        raise NotFound("Order not found")
    return o


@router.get("/reservations")
def shop_reservations(shop: OwnerShop, db: DB, scope: Literal["active", "history"] = "active"):
    reservation_service.expire_due(db)
    if scope == "active":
        rows = db.reservations.find({"shop_id": shop.id, "status": {"$in": RES_ACTIVE}}, sort=[("created_at", 1)])
    else:
        rows = db.reservations.find({"shop_id": shop.id, "status": {"$nin": RES_ACTIVE}},
                                    sort=[("updated_at", -1)], limit=100)
    return _res(db, rows)


@router.get("/reservations/{reservation_id}")
def shop_reservation(reservation_id: int, shop: OwnerShop, db: DB):
    reservation_service.expire_due(db)
    return _res(db, [_shop_reservation(db, shop, reservation_id)], with_events=True)[0]


@router.post("/reservations/{reservation_id}/{action}")
def act_on_reservation(reservation_id: int, action: Literal["confirm", "reject", "ready", "complete", "cancel"],
                       body: ShopTransitionIn, shop: OwnerShop, user: OwnerUser, db: DB):
    r = _shop_reservation(db, shop, reservation_id)
    r = reservation_service.transition(db, r.id, action, user, body.reason, code=body.code)
    return _res(db, [r], with_events=True)[0]


@router.get("/orders")
def shop_orders(shop: OwnerShop, db: DB, scope: Literal["active", "history"] = "active"):
    order_service.cancel_stale_pending(db)  # lazily, as reservations expire: right even between sweeps
    if scope == "active":
        rows = db.orders.find({"shop_id": shop.id, "status": {"$in": ORDER_OPEN}}, sort=[("created_at", 1)])
    else:
        rows = db.orders.find({"shop_id": shop.id, "status": {"$nin": ORDER_OPEN}},
                              sort=[("updated_at", -1)], limit=100)
    return _ord(db, rows)


@router.get("/orders/{order_id}")
def shop_order(order_id: int, shop: OwnerShop, db: DB):
    order_service.cancel_stale_pending(db)
    return _ord(db, [_shop_order(db, shop, order_id)], with_events=True)[0]


@router.post("/orders/{order_id}/{action}")
def act_on_order(order_id: int, action: Literal["confirm", "prepare", "dispatch", "deliver", "fail", "return",
                                                 "cancel"],
                 body: ShopTransitionIn, shop: OwnerShop, user: OwnerUser, db: DB):
    o = _shop_order(db, shop, order_id)
    o = order_service.transition(db, o.id, action, user, body.reason, code=body.code)
    return _ord(db, [o], with_events=True)[0]


# ------------------------------------------------------------------ bulk import (Excel / CSV)

class ImportRow(BaseModel):
    model_config = {"extra": "ignore"}
    row: int | None = None
    action: Literal["update", "add_catalog", "add_new", "skip"]
    product_id: int | None = None
    catalog_item_id: int | None = None
    name: str = Field(default="", max_length=200)
    brand: str | None = Field(default=None, max_length=80)
    category_slug: str | None = None
    quantity: int | None = Field(default=None, ge=0, le=100_000)
    price: float | None = Field(default=None, ge=0, le=10_000_000)
    mrp: float | None = Field(default=None, ge=0)


class ImportCommitIn(BaseModel):
    rows: list[ImportRow] = Field(min_length=1, max_length=importer.MAX_ROWS)


@router.get("/import/template", response_class=PlainTextResponse)
def import_template(_: OwnerShop):
    return PlainTextResponse(importer.TEMPLATE_CSV, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="nearshop-stock-template.csv"'})


@router.post("/import/preview")
def import_preview(shop: OwnerShop, db: DB, file: UploadFile = File(...)):
    data = file.file.read(settings.import_max_mb * 1024 * 1024 + 1)
    return importer.preview(db, shop, data, file.filename or "upload.csv")


@router.post("/import/commit")
def import_commit(body: ImportCommitIn, shop: OwnerShop, user: OwnerUser, db: DB):
    result = importer.commit(db, shop, [r.model_dump() for r in body.rows], user.id)
    _listings_changed()
    return result
