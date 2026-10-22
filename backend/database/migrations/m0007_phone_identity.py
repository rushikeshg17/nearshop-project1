"""Migration 7: phone identity (P4.1; plan section 8.5).

Phone sign-in (app/services/otp.py) makes the phone an identity: one account per phone, and accounts that
register by phone have no email. So:

  * duplicate phones are reported first. If two or more accounts share a phone number, the migration raises
    DuplicatePhones naming the user ids of each group (never the numbers) and changes nothing; the deploy fails
    and the previous release keeps serving. Support settles each group (the accounts' owners choose which keeps
    the number; the others' phone is cleared) and the deploy is run again.
  * uq_phone, a unique index on phone where phone is set (a non-empty string), is built.
  * uq_email becomes uq_email_set, unique on email where email is set: a unique index without a filter counts a
    missing email as a value, so it would allow only one account without an email. replace_index builds the
    new index before dropping the old, so one account per email holds at every moment.
  * the users validator stops requiring email (and accepts phone_verified_at, already written since P3.5).

The validator and indexes are those declared in database/schema.py, which ensure_schema applies again after this
migration. On an empty database (no users collection yet) there is nothing to check, and ensure_schema creates
everything. Idempotent: a second run finds no duplicates and every index already built.

Expand, then contract: everything here widens, except uq_phone. While the deploy runs, the previous release can
still try to give a second account a phone already in use; the index refuses that write with an error instead of
storing the duplicate. A rollback past this release cannot read accounts without an email, which exist only once
phone sign-in is on (a live provider configured), so roll back past it only before then."""
from __future__ import annotations

import logging

from pymongo.database import Database as MongoDatabase

from database.migrations import replace_index
from database.schema import EMAIL_INDEX, IS_SET, PHONE_INDEX, VALIDATORS

NUMBER = 7
NAME = "phone_identity"
COLLECTION = "users"
OLD_EMAIL_INDEX = "uq_email"

log = logging.getLogger(__name__)


class DuplicatePhones(RuntimeError):
    """Two or more accounts share a phone number. Nothing was changed."""

    def __init__(self, groups: list[list[int]]):
        self.groups = groups
        listed = "; ".join(", ".join(str(i) for i in ids) for ids in groups[:20])
        more = f" and {len(groups) - 20} more" if len(groups) > 20 else ""
        super().__init__(
            f"Migration 7 stopped: {len(groups)} phone number(s) are shared by more than one account. User ids "
            f"sharing a number, one group per number: {listed}{more}. Nothing was changed. Settle each group "
            f"(keep the number on one account and clear it on the others), then deploy again.")


def duplicate_phones(mongo: MongoDatabase) -> list[list[int]]:
    """The user ids of each group of accounts sharing a phone, in id order; empty when there are none. Read-only,
    and it returns ids only, so it can be run against any database to see what the migration would report."""
    rows = mongo[COLLECTION].aggregate([
        {"$match": {"phone": IS_SET}},
        {"$group": {"_id": "$phone", "ids": {"$push": "$_id"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$project": {"_id": 0, "ids": 1}},
    ])
    return sorted(sorted(row["ids"]) for row in rows)


def apply(mongo: MongoDatabase) -> None:
    if COLLECTION not in set(mongo.list_collection_names()):
        return  # a new database: ensure_schema creates the collection with the end state
    if groups := duplicate_phones(mongo):
        log.error("phone identity: %d phone number(s) shared by more than one account; nothing changed", len(groups))
        raise DuplicatePhones(groups)
    mongo[COLLECTION].create_indexes([PHONE_INDEX])  # an index already built is left as it is
    replace_index(mongo, COLLECTION, OLD_EMAIL_INDEX, EMAIL_INDEX)
    mongo.command("collMod", COLLECTION, validator=VALIDATORS[COLLECTION], validationLevel="strict",
                  validationAction="error")
    log.info("phone identity: uq_phone built, uq_email replaced by uq_email_set, email optional")
