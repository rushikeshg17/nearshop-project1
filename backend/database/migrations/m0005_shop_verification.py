"""Migration 5: shop verification (P3.3; plan section 8.1).

Shops gain verification_status, and customers see only shops whose status is verified with verified_until in
the future (app/models/core.py, live_shop_filter). Shops written before this release have no status, so they
are backfilled once:

  * is_verified true (verified before KYC existed): verified, decided now, verified_until 12 months on. They
    stay visible and come up for renewal like any other approval.
  * everything else: draft, hidden until the owner submits KYC evidence and an admin approves it.

The new fields are optional in the validator (database/schema.py), which ensure_schema applies after this
migration, together with the verification_queue index. Idempotent: only shops without a status are touched."""
from __future__ import annotations

from pymongo.database import Database as MongoDatabase

from app.core.database import utcnow
from app.models.core import VerificationStatus, months_after
from database.migrations import backfill

NUMBER = 5
NAME = "shop_verification"


def apply(mongo: MongoDatabase) -> None:
    now = utcnow()
    missing = {"verification_status": {"$exists": False}}
    backfill(mongo, "shops", {**missing, "is_verified": True},
             {"$set": {"verification_status": VerificationStatus.VERIFIED.value, "decided_at": now,
                       "verified_until": months_after(now)}})
    backfill(mongo, "shops", missing,
             {"$set": {"verification_status": VerificationStatus.DRAFT.value, "is_verified": False}})
