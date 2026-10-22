"""MongoDB schema for NearShop: collections, validation rules and indexes.

    uv run python -m database.migrate         # apply pending migrations, then create / update collections,
                                              # validators and indexes (what deploys run; see migrate.py)
    uv run python -m database.schema          # the same as database.migrate (kept for old habits)

`reset` drops every collection, so it is guarded: see `check_reset_allowed`. The seed and the
test suite are its only callers, and both pass a confirmation that repeats the database name.

MongoDB does not need migrations to add a field, but the rules that keep data correct still
live in the database, not only in application code:

  * `$jsonSchema` validators reject documents with missing fields, wrong types, unknown enum
    values, negative stock or prices, and out-of-range coordinates or ratings.
  * unique indexes enforce one account per email and one per phone, one listing per (shop, catalog
    item) and one review per purchase.
  * every query the app runs is backed by an index declared here.

How the relational model maps to documents
------------------------------------------
  shop_categories (join table)   -> `shops.category_ids` array (multikey index)
  order_items (child table)      -> `orders.items` embedded array (an order is one document)
  shops.lat / shops.lng          -> `shops.location` GeoJSON point (2dsphere index)
  products_fts (FTS5 table)      -> weighted text index on `products`
  search_history.lat / lng       -> `search_history.location` GeoJSON point (2dsphere index)
  text_embeddings (model, hash)  -> `text_embeddings._id` = "<model>:<sha256>"
  autoincrement primary keys     -> integer `_id` issued from the `counters` collection
  AVG(rating) per shop           -> `shops.rating_sum` / `rating_count` / `rating_customers`, recomputed
                                    from the counted reviews on every review change (app/services/reviews.py)

Everything else stays a reference by id (products -> shops, reservations -> products, ...):
those documents are read on their own, change independently and grow without bound, which is
exactly when embedding is the wrong choice.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from pymongo import ASCENDING, DESCENDING, GEOSPHERE, TEXT, IndexModel, MongoClient
from pymongo.database import Database as MongoDatabase
from pymongo.errors import OperationFailure

from app.models.core import KycType, VerificationStatus
from app.models.enums import (
    AnomalyStatus,
    InventoryReason,
    OrderStatus,
    ReservationStatus,
    Role,
    SaleSource,
)
from app.schemas.requests import CONSENT_PURPOSES, OTP_PURPOSES

log = logging.getLogger(__name__)

INT = ["int", "long"]
INT_N = ["int", "long", "null"]
NUM = ["int", "long", "double", "decimal"]
NUM_N = [*NUM, "null"]
STR = "string"
STR_N = ["string", "null"]
DATE = "date"
DATE_N = ["date", "null"]
BOOL = "bool"


def enum(e) -> dict:
    return {"enum": [m.value for m in e]}


def schema(required: list[str], **properties: dict | str | list) -> dict:
    props = {k: (v if isinstance(v, dict) else {"bsonType": v}) for k, v in properties.items()}
    return {"$jsonSchema": {"bsonType": "object", "required": ["_id", *required], "properties": {
        "_id": {"bsonType": INT}, **props}}}


POINT = {
    "bsonType": "object",
    "required": ["type", "coordinates"],
    "properties": {
        "type": {"enum": ["Point"]},
        "coordinates": {
            "bsonType": "array", "minItems": 2, "maxItems": 2,
            "items": [{"bsonType": NUM, "minimum": -180, "maximum": 180},  # longitude
                      {"bsonType": NUM, "minimum": -90, "maximum": 90}],  # latitude
        },
    },
}
RATING = {"bsonType": INT, "minimum": 1, "maximum": 5}
RATING_N = {"oneOf": [{"bsonType": "null"}, RATING]}
POSITIVE_INT = {"bsonType": INT, "minimum": 1}
NON_NEGATIVE = {"bsonType": NUM, "minimum": 0}

# The shop verification actions admins take (P3.3, app/api/routes/admin.py), each one row in admin_audit.
SHOP_AUDIT_ACTIONS = ("shop_phone_verified", "shop_verification_approve", "shop_verification_reject",
                      "shop_verification_reject_renewal", "shop_verification_suspend")

# What moderation records on a review and on a shop reply (P3.6, app/models/activity.py Moderation).
MODERATION: dict[str, dict | str | list] = dict(
    status={"enum": ["visible", "hidden", "removed"]}, report_count={"bsonType": INT, "minimum": 0},
    reported_at=DATE_N, triaged_at=DATE_N, decided_at=DATE_N, decision={"enum": ["keep", "hide", "remove", None]},
    decided_by=INT_N, decision_note={"bsonType": STR_N, "maxLength": 500},
)

VALIDATORS: dict[str, dict] = {
    # email is optional since P4.1 (migration 7): an account registered by phone has none, and leaves the field out.
    "users": schema(
        ["name", "password_hash", "role", "is_active", "created_at", "updated_at"],
        name=STR, email={"bsonType": STR, "pattern": "^[^@\\s]+@[^@\\s]+$"}, phone=STR_N, phone_verified_at=DATE_N,
        password_hash=STR, role=enum(Role), is_active=BOOL, last_login_at=DATE_N, home_lat=NUM_N, home_lng=NUM_N,
        home_label=STR_N, created_at=DATE, updated_at=DATE,
        # Revocable sessions and the admin second factor (P2.4, app/core/security.py). All optional: documents
        # written before them read the model's defaults.
        token_version={"bsonType": INT, "minimum": 0},
        sessions={"bsonType": "array", "maxItems": 10, "items": {
            "bsonType": "object", "required": ["id", "created_at", "last_seen_at", "expires_at"],
            "properties": {"id": {"bsonType": STR}, "created_at": {"bsonType": DATE},
                           "last_seen_at": {"bsonType": DATE}, "expires_at": {"bsonType": DATE},
                           "device": {"bsonType": STR, "maxLength": 60}}}},
        totp_secret_enc=STR_N, totp_enrolled_at=DATE_N,
        totp_recovery_hashes={"bsonType": "array", "maxItems": 10, "items": {"bsonType": STR}},
        totp_last_step={"bsonType": INT, "minimum": 0}, totp_failures={"bsonType": INT, "minimum": 0},
        totp_locked_until=DATE_N,
    ),
    "categories": schema(["slug", "name", "icon", "sort_order"], slug=STR, name=STR, icon=STR, sort_order=INT),
    "catalog_items": schema(
        ["slug", "name", "category_id", "specs", "tags"],
        slug=STR, name=STR, brand=STR_N, category_id=INT, subcategory=STR_N, mrp=NUM_N, typical_price=NUM_N,
        specs="object", tags={"bsonType": "array", "items": {"bsonType": STR}},
    ),
    "shops": schema(
        ["owner_id", "name", "slug", "address_line", "city", "location", "offers_pickup", "offers_delivery",
         "delivery_radius_km", "delivery_fee", "hold_minutes", "is_active", "is_verified", "category_ids",
         "created_at", "updated_at"],
        owner_id=INT, name=STR, slug=STR, address_line=STR, city=STR, location=POINT, offers_pickup=BOOL,
        offers_delivery=BOOL, delivery_radius_km=NON_NEGATIVE, delivery_fee=NON_NEGATIVE,
        free_delivery_above={"oneOf": [{"bsonType": "null"}, NON_NEGATIVE]},
        hold_minutes=POSITIVE_INT, is_active=BOOL, is_verified=BOOL, inventory_updated_at=DATE_N,
        category_ids={"bsonType": "array", "items": {"bsonType": INT}},
        rating_sum={"bsonType": INT, "minimum": 0}, rating_count={"bsonType": INT, "minimum": 0},
        rating_customers={"bsonType": INT, "minimum": 0},  # P3.5, optional: migration 6 recomputes it
        created_at=DATE, updated_at=DATE,
        # KYC and verification (P3.3). Optional, so shops written before it pass; migration 5 backfilled them.
        verification_status=enum(VerificationStatus),
        kyc_type={"enum": [*enum(KycType)["enum"], None]},
        kyc_number={"bsonType": STR_N, "maxLength": 40}, kyc_document_key=STR_N, kyc_storefront_key=STR_N,
        phone_verified_at=DATE_N, submitted_at=DATE_N, decided_at=DATE_N, decided_by=INT_N,
        rejection_reason={"bsonType": STR_N, "maxLength": 500}, verified_until=DATE_N,
        previous_kyc=["object", "null"],
    ),
    "products": schema(
        ["shop_id", "category_id", "name", "keywords", "price", "quantity", "low_stock_threshold", "is_active",
         "stock_updated_at", "created_at", "updated_at"],
        shop_id=INT, catalog_item_id=INT_N, category_id=INT, name=STR, brand=STR_N, sku=STR_N, keywords=STR,
        specs="object", price=NON_NEGATIVE, mrp=NUM_N,
        quantity={"bsonType": INT, "minimum": 0},  # stock can never go negative, whatever the code does
        low_stock_threshold={"bsonType": INT, "minimum": 0}, is_active=BOOL, stock_updated_at=DATE,
        created_at=DATE, updated_at=DATE,
    ),
    "inventory_events": schema(
        ["product_id", "shop_id", "delta", "quantity_after", "reason", "created_at"],
        product_id=INT, shop_id=INT, delta=INT, quantity_after={"bsonType": INT, "minimum": 0},
        reason=enum(InventoryReason), actor_id=INT_N, created_at=DATE,
    ),
    "reservations": schema(
        ["code", "customer_id", "shop_id", "product_id", "quantity", "unit_price", "status", "hold_minutes",
         "expires_at", "stock_held", "created_at", "updated_at"],
        code=STR, customer_id=INT, shop_id=INT, product_id=INT, quantity=POSITIVE_INT, unit_price=NON_NEGATIVE,
        status=enum(ReservationStatus), hold_minutes=POSITIVE_INT, expires_at=DATE, stock_held=BOOL,
        confirmed_at=DATE_N, ready_at=DATE_N, completed_at=DATE_N, closed_at=DATE_N, close_reason=STR_N,
        created_at=DATE, updated_at=DATE,
    ),
    "orders": schema(
        ["code", "customer_id", "shop_id", "status", "payment_method", "payment_status", "delivery_address",
         "delivery_lat", "delivery_lng", "contact_phone", "distance_km", "subtotal", "delivery_fee", "total",
         "stock_held", "items", "created_at", "updated_at"],
        code=STR, customer_id=INT, shop_id=INT, status=enum(OrderStatus), payment_method=STR,
        payment_status={"enum": ["unpaid", "paid", "refunded"]}, delivery_address=STR,
        delivery_lat={"bsonType": NUM, "minimum": -90, "maximum": 90},
        delivery_lng={"bsonType": NUM, "minimum": -180, "maximum": 180},
        subtotal=NON_NEGATIVE, delivery_fee=NON_NEGATIVE, total=NON_NEGATIVE, stock_held=BOOL,
        items={
            "bsonType": "array", "minItems": 1,
            "items": {"bsonType": "object", "required": ["product_id", "name", "unit_price", "quantity"],
                      "properties": {"product_id": {"bsonType": INT}, "name": {"bsonType": STR},
                                     "unit_price": NON_NEGATIVE, "quantity": POSITIVE_INT}},
        },
        delivered_at=DATE_N, closed_at=DATE_N, created_at=DATE, updated_at=DATE,
        delivery_code={"bsonType": STR_N, "pattern": "^[0-9]{4}$"},  # P3.5, optional
        code_failures={"bsonType": INT, "minimum": 0}, code_locked_until=DATE_N,  # P3.5, optional
    ),
    "status_events": schema(
        ["entity", "entity_id", "shop_id", "to_status", "actor_role", "created_at"],
        entity={"enum": ["reservation", "order"]}, entity_id=INT, shop_id=INT, from_status=STR_N, to_status=STR,
        actor_role={"enum": ["customer", "owner", "admin", "system"]}, actor_id=INT_N, created_at=DATE,
    ),
    "reviews": {"$jsonSchema": {
        **schema(
            ["customer_id", "shop_id", "reservation_id", "order_id", "rating", "created_at"],
            customer_id=INT, shop_id=INT, product_id=INT_N, rating=RATING, accuracy_rating=RATING_N,
            delivery_rating=RATING_N, comment=STR_N, created_at=DATE,
            # Review integrity (P3.5), optional: migration 6 sets counted and held on older reviews.
            counted=BOOL, held=BOOL, held_at=DATE_N, held_reason=STR_N,
            # Moderation (P3.6), optional: status (visible until moderation hides or removes it, P3.5), reports,
            # the admin's decision and the shop's one reply.
            **MODERATION, reply={"oneOf": [{"bsonType": "null"}, {
                "bsonType": "object", "required": ["text", "author_id", "created_at"],
                "properties": {"text": {"bsonType": STR, "minLength": 1, "maxLength": 1000}, "author_id": {
                    "bsonType": INT}, "created_at": {"bsonType": DATE}, "edited_at": {"bsonType": DATE_N},
                    **{k: v if isinstance(v, dict) else {"bsonType": v} for k, v in MODERATION.items()}}}]},
        )["$jsonSchema"],
        # A review belongs to exactly one purchase: a pickup reservation or a delivery order.
        "oneOf": [
            {"properties": {"reservation_id": {"bsonType": INT}, "order_id": {"bsonType": "null"}}},
            {"properties": {"reservation_id": {"bsonType": "null"}, "order_id": {"bsonType": INT}}},
        ],
    }},
    # Review and reply reports (P3.6): one per user per review or reply, enforced by uq_reporter.
    "review_reports": schema(
        ["review_id", "target", "reporter_id", "reason", "created_at"],
        review_id=INT, target={"enum": ["review", "reply"]}, reporter_id=INT,
        reason={"enum": ["fake", "abusive", "personal_info", "off_topic", "other"]},
        note={"bsonType": STR_N, "maxLength": 500}, created_at=DATE,
    ),
    "notifications": schema(["user_id", "kind", "title", "created_at"],
                            user_id=INT, kind=STR, title=STR, read_at=DATE_N, created_at=DATE),
    "search_history": schema(
        ["query", "normalized_query", "results_count", "created_at"],
        user_id=INT_N, query=STR, normalized_query=STR, results_count={"bsonType": INT, "minimum": 0},
        location={"oneOf": [{"bsonType": "null"}, POINT]}, created_at=DATE,
    ),
    "sales_history": schema(
        ["shop_id", "product_id", "basket_id", "quantity", "unit_price", "source", "is_demo", "sold_at"],
        shop_id=INT, product_id=INT, catalog_item_id=INT_N, category_id=INT_N, customer_id=INT_N, basket_id=STR,
        quantity=POSITIVE_INT, unit_price=NON_NEGATIVE, source=enum(SaleSource), is_demo=BOOL, sold_at=DATE,
    ),
    "model_runs": schema(["name", "algorithm", "metrics", "created_at"],
                         name=STR, algorithm=STR, metrics="object", created_at=DATE),
    "recommendations": schema(
        ["antecedents", "consequents", "antecedent_key", "support", "confidence", "lift", "model_run_id"],
        antecedents={"bsonType": "array", "minItems": 1, "items": {"bsonType": INT}},
        consequents={"bsonType": "array", "minItems": 1, "items": {"bsonType": INT}},
        antecedent_key=STR, support=NUM, confidence=NUM, lift=NUM, model_run_id=INT,
    ),
    "demand_forecasts": schema(
        ["product_id", "shop_id", "predicted_7d", "daily_rate", "trend", "confidence", "model_run_id"],
        product_id=INT, shop_id=INT, predicted_7d=NON_NEGATIVE, daily_rate=NON_NEGATIVE,
        trend={"enum": ["rising", "steady", "falling"]}, confidence={"enum": ["low", "medium", "high"]},
        model_run_id=INT,
    ),
    "price_anomalies": schema(
        ["product_id", "shop_id", "catalog_item_id", "price", "reference_price", "deviation_pct", "score",
         "direction", "peer_count", "status", "model_run_id", "created_at"],
        product_id=INT, shop_id=INT, catalog_item_id=INT, price=NON_NEGATIVE, reference_price=NON_NEGATIVE,
        direction={"enum": ["high", "low"]}, status=enum(AnomalyStatus), reviewed_by=INT_N, reviewed_at=DATE_N,
        model_run_id=INT, created_at=DATE,
    ),
    # Background jobs (app/jobs.py). One lease per job, _id the job name: a process runs the job only while
    # it holds the lease (holder, until expires_at); last_run_at is the job's heartbeat.
    "job_leases": {"$jsonSchema": {"bsonType": "object", "required": ["_id", "holder", "expires_at"], "properties": {
        "_id": {"bsonType": STR}, "holder": {"bsonType": STR}, "acquired_at": {"bsonType": DATE},
        "expires_at": {"bsonType": DATE}, "last_run_at": {"bsonType": DATE_N},
        "last_result": {"bsonType": ["object", "null"]},
    }}},
    # Retraining requests, processed by the retrain cron job outside the API. `active` is set only while a
    # request is queued or running, and `nightly_for` (the slot's date) only on nightly runs: both unique.
    "retrain_requests": schema(
        ["kind", "requested_at", "status"],
        kind={"enum": ["manual", "nightly"]}, requested_by=INT_N, requested_at=DATE,
        status={"enum": ["queued", "running", "succeeded", "failed"]}, started_at=DATE_N, finished_at=DATE_N,
        outcome={"bsonType": ["object", "null"]}, active={"enum": ["retrain"]},
        nightly_for={"bsonType": STR, "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    ),
    # Storage budget (P2.2, app/services/analytics.py): one snapshot per IST day, _id the day (YYYY-MM-DD),
    # recorded by the snapshot_storage job under its lease. Sizes in bytes; collections lists each
    # collection's documents, data_bytes (uncompressed BSON) and index_bytes, largest first.
    "storage_snapshots": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "taken_at", "limit_mb", "data_bytes", "index_bytes", "total_bytes", "collections"],
        "properties": {
            "_id": {"bsonType": STR, "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}, "taken_at": {"bsonType": DATE},
            "limit_mb": POSITIVE_INT, "data_bytes": {"bsonType": INT, "minimum": 0},
            "index_bytes": {"bsonType": INT, "minimum": 0}, "total_bytes": {"bsonType": INT, "minimum": 0},
            "collections": {"bsonType": "array", "items": {
                "bsonType": "object", "required": ["name", "documents", "data_bytes", "index_bytes"],
                "properties": {"name": {"bsonType": STR}, "documents": {"bsonType": INT, "minimum": 0},
                               "data_bytes": {"bsonType": INT, "minimum": 0},
                               "index_bytes": {"bsonType": INT, "minimum": 0}}}},
        },
    }},
    # Rate-limit counters (P2.3, app/core/ratelimit.py; plan section 8.3). One document per rule, key and fixed
    # window: _id "<rule>:<window start, epoch seconds>:<keyed hash of the key>", count, and expires_at, the
    # window's end, after which the TTL index removes it. A cooldown is "<rule>:cooldown:<hash>" with count 0,
    # blocking until expires_at. The key itself (an IP address, a user id, an email) is never stored.
    "rate_limits": {"$jsonSchema": {"bsonType": "object", "required": ["_id", "rule", "count", "expires_at"],
                                    "properties": {
        "_id": {"bsonType": STR}, "rule": {"bsonType": STR}, "count": {"bsonType": INT, "minimum": 0},
        "expires_at": {"bsonType": DATE},
    }}},
    # Admin audit trail (P2.4): one row per sensitive admin action. Who (admin_id), when (created_at), what
    # (action), on whom (target_user_id for reveal_email; target_shop_id for the shop verification actions,
    # P3.3) and why (reason). The TTL index on created_at removes a row after a year.
    "admin_audit": {"$jsonSchema": {
        **schema(["admin_id", "action", "reason", "created_at"],
                 admin_id=INT, action={"enum": ["reveal_email", *SHOP_AUDIT_ACTIONS]}, target_user_id=INT,
                 target_shop_id=INT, reason={"bsonType": STR, "minLength": 10, "maxLength": 500},
                 created_at=DATE)["$jsonSchema"],
        "oneOf": [{"properties": {"action": {"enum": ["reveal_email"]}}, "required": ["target_user_id"]},
                  {"properties": {"action": {"enum": list(SHOP_AUDIT_ACTIONS)}}, "required": ["target_shop_id"]}],
    }},
    # Consent records (P3.1, plan section 8.5): append-only, one row per purpose and one for the 18-or-older
    # declaration (purpose age_18_plus) at each registration, against the notice version then current. A later
    # change of mind is a new row; the newest row for a (user, purpose) is the one in force. Never updated.
    "consents": schema(
        ["user_id", "purpose", "notice_version", "granted", "at", "source"],
        user_id=INT, purpose={"enum": list(CONSENT_PURPOSES)},
        notice_version={"bsonType": STR, "minLength": 1, "maxLength": 40}, granted=BOOL, at=DATE,
        source={"enum": ["web"]},
    ),
    # Grievances (P3.8, app/api/routes/grievances.py): acknowledged on receipt, resolution due 30 days later
    # (due_at). contact is the complainant's email (lower-cased) or 10-digit mobile number; order_code is the order
    # or reservation code they named, with order_id or reservation_id when it matched one.
    "grievances": schema(
        ["ticket", "category", "description", "contact", "contact_kind", "status", "source", "created_at",
         "acknowledged_at", "due_at", "updated_at"],
        ticket={"bsonType": STR, "pattern": r"^G-[A-HJ-NP-Z2-9]{6}$"},
        category={"enum": ["order", "shop_conduct", "privacy", "account", "other"]},
        description={"bsonType": STR, "minLength": 1, "maxLength": 2000},
        contact={"bsonType": STR, "minLength": 1, "maxLength": 254}, contact_kind={"enum": ["email", "phone"]},
        user_id=INT_N, order_code={"bsonType": STR_N, "maxLength": 20}, order_id=INT_N, reservation_id=INT_N,
        status={"enum": ["open", "acknowledged", "in_progress", "resolved"]}, source={"enum": ["web"]},
        created_at=DATE, acknowledged_at=DATE_N, due_at=DATE, in_progress_at=DATE_N, resolved_at=DATE_N,
        resolved_by=INT_N, resolution={"bsonType": STR_N, "maxLength": 2000}, updated_at=DATE,
    ),
    # Phone one-time codes (P4.1, app/services/otp.py; plan section 8.4). One code per phone and purpose: _id is a
    # keyed hash of the two, so a new request replaces the last code. The code itself is stored only as a keyed hash
    # (code_hash), never the phone (phone_hash). A code is valid until expires_at (5 minutes), for at most 5
    # attempts, and once (used_at); the TTL index removes it 10 minutes after created_at. user_id: the signed-in
    # user a verify code was sent for.
    "otp_codes": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "phone_hash", "purpose", "code_hash", "attempts", "created_at", "expires_at"],
        "properties": {
            "_id": {"bsonType": STR}, "phone_hash": {"bsonType": STR}, "purpose": {"enum": list(OTP_PURPOSES)},
            "code_hash": {"bsonType": STR}, "user_id": {"bsonType": INT_N}, "attempts": {"bsonType": INT, "minimum": 0},
            "created_at": {"bsonType": DATE}, "expires_at": {"bsonType": DATE}, "used_at": {"bsonType": DATE_N},
        },
    }},
    # Every code send (P4.1): which channel, the cost estimate in rupees and the outcome (sent, failed, or capped
    # when the daily cap refused it), against the keyed phone hash. Kept 30 days (TTL on at).
    "otp_sends": {"$jsonSchema": {
        "bsonType": "object", "required": ["_id", "phone_hash", "purpose", "outcome", "cost_estimate", "at"],
        "properties": {
            "phone_hash": {"bsonType": STR}, "purpose": {"enum": list(OTP_PURPOSES)},
            "channel": {"enum": ["whatsapp", "sms", "console", None]},
            "outcome": {"enum": ["sent", "failed", "capped"]}, "cost_estimate": NON_NEGATIVE,
            "error": {"bsonType": STR_N, "maxLength": 80}, "at": {"bsonType": DATE},
        },
    }},
    # The global daily cap's counter (P4.1): one document per IST day, _id YYYY-MM-DD, with the sends counted and
    # when the 80% warning was reported (once a day). The TTL index removes it after expires_at.
    "otp_daily": {"$jsonSchema": {
        "bsonType": "object", "required": ["_id", "count", "expires_at"],
        "properties": {
            "_id": {"bsonType": STR, "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}, "count": {"bsonType": INT, "minimum": 0},
            "warned_at": {"bsonType": DATE_N}, "expires_at": {"bsonType": DATE},
        },
    }},
    # No validators: `counters` ({_id: collection name, seq}) and `text_embeddings` (binary vector cache).
    "counters": {},
    "text_embeddings": {},
}

A, D = ASCENDING, DESCENDING
ADMIN_AUDIT_KEEP_SECONDS = 365 * 24 * 60 * 60  # admin_audit rows are kept for one year
# Retention (P3.2; plan section 8.4; docs/legal/retention.md). Built on existing databases by migration 4.
SEARCH_HISTORY_KEEP_SECONDS = 180 * 24 * 60 * 60  # search_history rows are deleted after 180 days
NOTIFICATIONS_KEEP_SECONDS = 90 * 24 * 60 * 60  # notifications are deleted after 90 days
SEARCH_HISTORY_TTL = IndexModel([("created_at", ASCENDING)], expireAfterSeconds=SEARCH_HISTORY_KEEP_SECONDS,
                                name="created_ttl_180d")  # replaced the plain index "created" (migration 4)
NOTIFICATIONS_TTL = IndexModel([("created_at", ASCENDING)], expireAfterSeconds=NOTIFICATIONS_KEEP_SECONDS,
                               name="created_ttl_90d")
IS_INT = {"$type": "number"}  # partial-index filter: "this optional reference is set"
OTP_CODES_KEEP_SECONDS = 10 * 60  # otp_codes: removed 10 minutes after they were created (plan section 8.4)
OTP_SENDS_KEEP_SECONDS = 30 * 24 * 60 * 60  # otp_sends: the send log is kept 30 days
# One account per email and one per phone, among the accounts that have one (migration 7 built both; uq_email_set
# replaced uq_email, which also counted a missing email as a value). Phones are stored as their 10 digits. The
# filter is "a string after the empty one": it leaves out a missing or null value, and, unlike a $type filter, an
# equality lookup ({"email": ...}, {"phone": ...}) implies it, so sign-in lookups still use these indexes.
IS_SET = {"$gt": ""}
EMAIL_INDEX = IndexModel([("email", ASCENDING)], unique=True, name="uq_email_set",
                         partialFilterExpression={"email": IS_SET})
PHONE_INDEX = IndexModel([("phone", ASCENDING)], unique=True, name="uq_phone", partialFilterExpression={"phone": IS_SET})

INDEXES: dict[str, list[IndexModel]] = {
    "users": [
        EMAIL_INDEX,
        PHONE_INDEX,
        IndexModel([("role", A), ("created_at", D)], name="role_created"),
        # Deletions waiting for orders in flight (P3.2): the process_deletions job reads only these.
        IndexModel([("deletion_due_at", A)], name="deletion_due",
                   partialFilterExpression={"deletion_due_at": {"$type": "date"}}),
    ],
    "categories": [
        IndexModel([("slug", A)], unique=True, name="uq_slug"),
        IndexModel([("sort_order", A), ("name", A)], name="sort_order"),
    ],
    "catalog_items": [
        IndexModel([("slug", A)], unique=True, name="uq_slug"),
        IndexModel([("category_id", A), ("name", A)], name="category_name"),
    ],
    "shops": [
        IndexModel([("slug", A)], unique=True, name="uq_slug"),
        IndexModel([("owner_id", A)], name="owner"),
        IndexModel([("location", GEOSPHERE)], name="location_2dsphere"),
        IndexModel([("category_ids", A)], name="categories"),
        IndexModel([("verification_status", A), ("submitted_at", A)], name="verification_queue"),  # P3.3
    ],
    "products": [
        IndexModel([("shop_id", A), ("is_active", A), ("quantity", A)], name="shop_active_stock"),
        IndexModel([("shop_id", A), ("catalog_item_id", A)], unique=True, name="uq_shop_catalog_item",
                   partialFilterExpression={"catalog_item_id": IS_INT}),
        IndexModel([("catalog_item_id", A), ("is_active", A)], name="catalog_item_active"),
        IndexModel([("category_id", A), ("is_active", A)], name="category_active"),
        # Keyword half of hybrid search (replaces SQLite FTS5): weighted, stemmed text index.
        IndexModel([("name", TEXT), ("brand", TEXT), ("keywords", TEXT), ("description", TEXT)],
                   weights={"name": 10, "brand": 6, "keywords": 4, "description": 1},
                   default_language="english", name="text_search"),
    ],
    "inventory_events": [
        IndexModel([("shop_id", A), ("created_at", D)], name="shop_created"),
        IndexModel([("product_id", A), ("_id", D)], name="product_latest"),
        IndexModel([("created_at", D)], name="created"),
    ],
    "reservations": [
        IndexModel([("code", A)], unique=True, name="uq_code"),
        IndexModel([("shop_id", A), ("status", A), ("created_at", A)], name="shop_status_created"),
        IndexModel([("customer_id", A), ("status", A), ("created_at", D)], name="customer_status_created"),
        IndexModel([("status", A), ("expires_at", A)], name="status_expires"),  # the expiry sweep
        IndexModel([("product_id", A), ("status", A)], name="product_status"),
        # A customer can hold only one active reservation per listing, guaranteed by the database.
        IndexModel([("customer_id", A), ("product_id", A)], unique=True, name="uq_active_customer_product",
                   partialFilterExpression={"status": {"$in": ["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"]}}),
    ],
    "orders": [
        IndexModel([("code", A)], unique=True, name="uq_code"),
        IndexModel([("shop_id", A), ("status", A), ("created_at", A)], name="shop_status_created"),
        IndexModel([("customer_id", A), ("status", A), ("created_at", D)], name="customer_status_created"),
        IndexModel([("status", A), ("created_at", A)], name="status_created"),  # stale-pending sweep
    ],
    "status_events": [
        IndexModel([("entity", A), ("entity_id", A), ("created_at", A)], name="entity_timeline"),
        IndexModel([("shop_id", A), ("created_at", D)], name="shop_created"),
    ],
    "reviews": [
        IndexModel([("reservation_id", A)], unique=True, name="uq_reservation",
                   partialFilterExpression={"reservation_id": IS_INT}),
        IndexModel([("order_id", A)], unique=True, name="uq_order", partialFilterExpression={"order_id": IS_INT}),
        IndexModel([("shop_id", A), ("created_at", D)], name="shop_created"),
        IndexModel([("product_id", A), ("created_at", D)], name="product_created"),
        IndexModel([("customer_id", A)], name="customer"),
        # The moderation queue and the monthly SLA (P3.6): reported reviews, held reviews and reported replies.
        # The SLA's date ranges use reported, held and reply_reported; the queue uses reported, held_open
        # (reviews held now) and reply_reported.
        IndexModel([("reported_at", A)], name="reported", partialFilterExpression={"reported_at": {"$type": "date"}}),
        IndexModel([("held_at", A)], name="held", partialFilterExpression={"held_at": {"$type": "date"}}),
        IndexModel([("held", A)], name="held_open", partialFilterExpression={"held": True}),
        IndexModel([("reply.reported_at", A)], name="reply_reported",
                   partialFilterExpression={"reply.reported_at": {"$type": "date"}}),
    ],
    "review_reports": [
        IndexModel([("review_id", A), ("target", A), ("reporter_id", A)], unique=True, name="uq_reporter"),
        IndexModel([("reporter_id", A), ("created_at", D)], name="reporter_created"),
    ],
    "notifications": [
        IndexModel([("user_id", A), ("created_at", D)], name="user_created"),
        IndexModel([("user_id", A), ("read_at", A)], name="user_read"),
        NOTIFICATIONS_TTL,
    ],
    "search_history": [
        IndexModel([("normalized_query", A), ("results_count", A)], name="query_results"),
        SEARCH_HISTORY_TTL,
        IndexModel([("user_id", A), ("created_at", D)], name="user_created"),
        IndexModel([("location", GEOSPHERE)], name="location_2dsphere"),
    ],
    "sales_history": [
        IndexModel([("shop_id", A), ("sold_at", D)], name="shop_sold"),
        IndexModel([("product_id", A), ("sold_at", D)], name="product_sold"),
        IndexModel([("sold_at", D)], name="sold"),
        IndexModel([("customer_id", A), ("sold_at", D)], name="customer_sold",
                   partialFilterExpression={"customer_id": IS_INT}),
        IndexModel([("catalog_item_id", A)], name="catalog_item"),
    ],
    "model_runs": [IndexModel([("name", A), ("created_at", D)], name="name_created")],
    "recommendations": [IndexModel([("antecedent_key", A)], name="antecedent_key")],
    "demand_forecasts": [
        IndexModel([("product_id", A)], unique=True, name="uq_product"),
        IndexModel([("shop_id", A)], name="shop"),
    ],
    "price_anomalies": [
        IndexModel([("status", A), ("score", D)], name="status_score"),
        IndexModel([("shop_id", A), ("status", A)], name="shop_status"),
    ],
    "text_embeddings": [IndexModel([("model", A)], name="model")],
    "retrain_requests": [
        # At most one request queued or running, and one nightly run per night, enforced by the database.
        IndexModel([("active", A)], unique=True, name="uq_active",
                   partialFilterExpression={"active": {"$type": "string"}}),
        IndexModel([("nightly_for", A)], unique=True, name="uq_nightly_for",
                   partialFilterExpression={"nightly_for": {"$type": "string"}}),
        IndexModel([("status", A), ("_id", A)], name="status_id"),  # the cron's claim; the active request
        IndexModel([("kind", A), ("requested_at", D)], name="kind_requested"),  # the daily limit
    ],
    # MongoDB's TTL monitor deletes a counter about a minute after its window ends; the limiter never relies on
    # that, because a new window has a new _id and a cooldown is checked against expires_at.
    "rate_limits": [IndexModel([("expires_at", A)], expireAfterSeconds=0, name="expires_ttl")],
    "consents": [
        IndexModel([("user_id", A), ("purpose", A), ("at", D)], name="user_purpose_at"),  # the row in force
        IndexModel([("purpose", A), ("at", D)], name="purpose_at"),
    ],
    "grievances": [
        IndexModel([("ticket", A)], unique=True, name="uq_ticket"),  # the status lookup
        IndexModel([("status", A), ("created_at", A)], name="status_created"),  # the admin queue, oldest first
        IndexModel([("user_id", A), ("created_at", D)], name="user_created",
                   partialFilterExpression={"user_id": IS_INT}),  # a signed-in user's own grievances
        IndexModel([("order_id", A)], name="order", partialFilterExpression={"order_id": IS_INT}),  # retention holds
    ],
    "otp_codes": [IndexModel([("created_at", A)], expireAfterSeconds=OTP_CODES_KEEP_SECONDS, name="created_ttl_10m")],
    "otp_sends": [IndexModel([("at", A)], expireAfterSeconds=OTP_SENDS_KEEP_SECONDS, name="at_ttl_30d")],
    "otp_daily": [IndexModel([("expires_at", A)], expireAfterSeconds=0, name="expires_ttl")],
    "admin_audit": [
        IndexModel([("created_at", A)], expireAfterSeconds=ADMIN_AUDIT_KEEP_SECONDS, name="created_ttl_1y"),
        IndexModel([("target_user_id", A), ("created_at", D)], name="target_created"),
        IndexModel([("admin_id", A), ("created_at", D)], name="admin_created"),
    ],
}


# MongoDB's codes for an index that clashes with one already built: IndexOptionsConflict (same name or
# keys, other options; or the same index under another name) and IndexKeySpecsConflict (same name, other keys).
INDEX_CONFLICT_CODES = (85, 86)


class SchemaConflict(RuntimeError):
    """An index declared in INDEXES clashes with one already in the database. Nothing was dropped."""


def ensure_schema(mongo: MongoDatabase) -> None:
    """Create missing collections, apply validators and build missing indexes. Safe to run repeatedly.

    It only adds: it never drops an index, and an index the code no longer declares stays until a
    migration removes it. A declared index that clashes with an existing one (same name with other keys
    or options, or the same keys and options under another name) raises SchemaConflict naming the
    collection and the index, and every existing index stays in place. To change an index, give it a
    new name and write a migration with replace_index (database/migrations). The migration runner
    (database/migrate.py) calls this after applying pending migrations; nothing else should need to."""
    existing = set(mongo.list_collection_names())
    for name, validator in VALIDATORS.items():
        if name not in existing:
            mongo.create_collection(name, **({"validator": validator, "validationLevel": "strict",
                                              "validationAction": "error"} if validator else {}))
        elif validator:
            mongo.command("collMod", name, validator=validator, validationLevel="strict", validationAction="error")
        for model in INDEXES.get(name, ()):
            # One index per command, so a conflict names the index that caused it.
            try:
                mongo[name].create_indexes([model])
            except OperationFailure as exc:
                if exc.code not in INDEX_CONFLICT_CODES:
                    raise
                index_name = model.document["name"]
                reason = (exc.details or {}).get("errmsg") or str(exc)
                raise SchemaConflict(
                    f"Index {index_name!r} on collection {name!r} conflicts with an index already in the "
                    f"database (MongoDB error {exc.code}: {reason}). No index was dropped. To change an "
                    f"index's keys or options, declare it under a new name and add a migration that uses "
                    f"replace_index (see database/migrations).") from exc


# Environments in which reset (and the seed through it) may run. Staging and production never may.
RESET_ENVIRONMENTS = ("development", "test", "demo")
# In development and test, reset runs only against a MongoDB on the machine itself (or the CI service
# host mongo). Seeding a hosted demo cluster takes NEARSHOP_ENVIRONMENT=demo.
LOCAL_ONLY_ENVIRONMENTS = ("development", "test")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "mongo"})
# MongoDB Atlas domains. An Atlas cluster's SRV name is <cluster>.<project>.mongodb.net, but its
# standard (mongodb://) connection string names member hosts such as ac-xyz-shard-00-00.<project>.mongodb.net,
# which are not under the SRV name. So a protected Atlas name also protects its project domain.
ATLAS_DOMAINS = ("mongodb.net", "mongodbgov.net")


class ResetRefused(RuntimeError):
    """reset was asked to wipe a database the environment guard protects. Nothing was written."""


def uri_hosts(uri: str) -> list[str]:
    """Host names in a MongoDB connection string: lower-case, without credentials, ports or options.

    A mongodb+srv URI gives its SRV name (cluster0.abcd.mongodb.net). Pure: nothing is resolved."""
    rest = uri.strip().split("://", 1)[-1]
    # Credentials cannot hold an unescaped / ? or @, so the host list ends at the first / or ?.
    rest = re.split(r"[/?]", rest, maxsplit=1)[0].rsplit("@", 1)[-1]
    hosts = []
    for part in rest.split(","):
        part = part.strip().lower()
        if part.startswith("["):  # IPv6: [::1]:27017
            hosts.append(part[1:].split("]", 1)[0])
        elif part:
            hosts.append(part.split(":", 1)[0])
    return hosts


def client_hosts(client: MongoClient) -> list[str]:
    """Host names a client was built with (its seed list, or for mongodb+srv its SRV name until it has
    resolved it, then the member hosts). Read from the client's topology, so nothing is connected."""
    try:
        return sorted({str(host).lower() for host, _port in client.topology_description.server_descriptions()})
    except Exception:  # an unusual client: the configured URI is still checked
        return []


def protected_domains(protected_hosts: list[str]) -> list[str]:
    """The protected hosts plus, for an Atlas cluster name, its project domain (see ATLAS_DOMAINS)."""
    domains = []
    for host in (h.strip().lower().rstrip(".") for h in protected_hosts):
        if not host:
            continue
        domains.append(host)
        for atlas in ATLAS_DOMAINS:
            labels = host.removesuffix("." + atlas).split(".")
            if host.endswith("." + atlas) and len(labels) >= 2:
                domains.append(f"{labels[-1]}.{atlas}")
    return list(dict.fromkeys(domains))


def _is_protected(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def check_reset_allowed(*, environment: str, mongodb_uri: str, db_name: str, production_db: str,
                        protected_hosts: list[str], confirm: str | None,
                        extra_hosts: Iterable[str] = ()) -> str | None:
    """Say why `db_name` must not be wiped, or return None when reset may run.

    All of these must hold: the environment is development, test or demo; in development and test,
    every host is local (LOCAL_HOSTS); the database is not the production database name; no host is
    (under) a protected host or, for an Atlas cluster name, its project domain; and the operator's
    confirmation repeats the database name. The hosts are those of `mongodb_uri` plus `extra_hosts`
    (the hosts of the client actually used). Pure: it reads no settings and touches no database, so
    callers check it before creating any database handle."""
    if environment not in RESET_ENVIRONMENTS:
        return (f"NEARSHOP_ENVIRONMENT is {environment!r}; reset and the seed run only in "
                f"{', '.join(RESET_ENVIRONMENTS)}.")
    if db_name == production_db:
        return f"database {db_name!r} is the production database name (NEARSHOP_PRODUCTION_MONGODB_DB)."
    hosts = list(dict.fromkeys([*uri_hosts(mongodb_uri), *(h.lower() for h in extra_hosts)]))
    if not hosts:
        return "NEARSHOP_MONGODB_URI names no host."
    domains = protected_domains(protected_hosts)
    for host in hosts:
        if _is_protected(host, domains):
            return f"MongoDB host {host!r} is listed in NEARSHOP_PROTECTED_HOSTS (a staging or production cluster)."
    if environment in LOCAL_ONLY_ENVIRONMENTS:
        for host in hosts:
            if host not in LOCAL_HOSTS:
                return (f"MongoDB host {host!r} is not local. In {environment}, reset and the seed run only "
                        f"against {', '.join(sorted(LOCAL_HOSTS))}; a hosted demo cluster needs "
                        f"NEARSHOP_ENVIRONMENT=demo.")
    if not confirm:
        return f"no confirmation given. Repeat the database name to wipe it: --confirm-db {db_name}"
    if confirm != db_name:
        return f"the confirmation {confirm!r} does not match the database name {db_name!r}."
    return None


def reset_problem(db_name: str, confirm: str | None, *, client: MongoClient | None = None) -> str | None:
    """`check_reset_allowed` for the configured environment, URI and protected hosts, plus the hosts of
    `client` when given (the client that would run the reset)."""
    from app.core.config import settings

    return check_reset_allowed(environment=settings.environment, mongodb_uri=settings.mongodb_uri,
                               db_name=db_name, production_db=settings.production_mongodb_db,
                               protected_hosts=settings.protected_host_list, confirm=confirm,
                               extra_hosts=client_hosts(client) if client is not None else ())


def reset(mongo: MongoDatabase, *, confirm: str | None) -> None:
    """Drop every NearShop collection, the migration records and the migration lock, then rebuild the
    empty schema with the migration runner (used by the seed script and tests).

    Raises ResetRefused, before any write, unless `check_reset_allowed` passes for this database, the
    hosts of the client behind `mongo` and the configured URI, with `confirm` as the operator's
    confirmation."""
    if problem := reset_problem(mongo.name, confirm, client=mongo.client):
        raise ResetRefused(f"Reset refused; nothing was written. {problem}")
    from database import migrate  # imports this module

    for name in (*VALIDATORS, migrate.MIGRATIONS_COLLECTION, migrate.LOCK_COLLECTION):
        mongo.drop_collection(name)
    migrate.run(mongo)  # the empty database gets every migration recorded, as a deployed one would


if __name__ == "__main__":
    # The migration runner, with its lock, its records and its refusal to write to a hosted database
    # from development or test: ensure_schema on its own is never run from the command line.
    from database import migrate

    raise SystemExit(migrate.main())
