"""Operator tools for the credential lockdown (plan step 0-D). Run from backend/:

    uv run python -m database.admin_tasks report
    uv run python -m database.admin_tasks demo-check
    uv run python -m database.admin_tasks rotate-demo
    uv run python -m database.admin_tasks deactivate-user 42
    uv run python -m database.admin_tasks list-admins
    uv run python -m database.admin_tasks create-admin --name "Asha Rao" --email asha@example.com
    uv run python -m database.admin_tasks backfill-thumbnails
    uv run python -m database.admin_tasks search-index plan --tier M0
    uv run python -m database.admin_tasks search-index create --tier Flex [--quantization scalar] [--production]
    uv run python -m database.admin_tasks search-index status --tier Flex
    uv run python -m database.admin_tasks search-index drop [--production]
    uv run python -m database.admin_tasks consent-report --since 2027-01-17
    uv run python -m database.admin_tasks facilitated-sales --fy 2027-28
    uv run python -m database.admin_tasks demand-audit status
    uv run python -m database.admin_tasks demand-audit label
    uv run python -m database.admin_tasks demand-audit failures
    uv run python -m database.admin_tasks demand-audit build

They act on the database in NEARSHOP_MONGODB_URI / NEARSHOP_MONGODB_DB, the same one the API uses,
without wiping anything (the seed is the tool that replaces a database).

  report           read-only. Database name, users by role, the ids and roles of accounts outside the
                   demo domain (the seed's generated customers apart from everyone else), last sign-in
                   of every admin and demo account, and whether each demo
                   account's stored hash still verifies against the old public demo password. That is a
                   local Argon2 check; nothing signs in. The old password is not kept in the repo (only
                   its SHA-256, in database/seed/seed.py), so the report asks for it with hidden input,
                   or reads NEARSHOP_RETIRED_DEMO_PASSWORD, and checks it against that digest first.
  demo-check       read-only (P7.1). Counts, in every collection of the database, the documents flagged
                   is_demo (the seed's sales and the demand forecasts trained on them), and the users on the
                   nearshop.demo domain or a subdomain of it. Exits 0 when both are 0, the state production must
                   be in, and 1 otherwise. Prints counts only.
  rotate-demo      gives every account on the nearshop.demo domain (or a subdomain of it, such as
                   shops.nearshop.demo) its own random password and prints each one once, with the
                   account's id, role and name, for the team password manager.
  deactivate-user  sets is_active to false on one user id and ends every session of that user at once.
  list-admins      ids, created dates, status and second-factor enrolment of admin accounts.
  create-admin     the only way to make an admin (P2.4). Creates the account with a random password and
                   enrols its second factor: it prints, once, the password, the otpauth:// provisioning URI
                   for an authenticator app and 10 single-use recovery codes (stored only as digests). It
                   needs NEARSHOP_TOTP_KEY (the key the TOTP secret is encrypted with, the same value the
                   API has) and refuses to create a fourth active admin.
  backfill-thumbnails  creates the 320 px thumbnail (P2.7) of every shop and product photo that lacks one,
                   in the photo storage the API uses (NEARSHOP_MEDIA_BACKEND), reading the photo keys from
                   the database in batches (--batch-size, default 100). It never changes the database and
                   skips every photo whose thumbnail exists, so it is safe to run again; at the end it
                   prints how many photos still lack one and the median thumbnail size.
  search-index     the Atlas Search and Atlas Vector Search indexes of P5.7 (app/ai/atlas_search.py;
                   docs/search/engine-decision.md), the only way to make or remove them on staging and production.
                   plan (read-only): each index's definition size against the 3 KB limit and its synonym
                   mappings, and the index count against the tier's limit (--tier: M0 allows 3, Flex 10).
                   create: fills the synonym source (search_synonyms) and listing_vectors, then builds the
                   missing indexes, the vector index with the dimensions of the vectors written. It refuses to
                   go above the tier's limit or NearShop's 3, counted over the whole cluster as Atlas counts
                   them, and in production it needs --production and runs only from 21:00 to 06:00 IST. status
                   (read-only): every search and vector index of the cluster, with its state, against the
                   tier's limit and NearShop's 3. drop: removes the 3
                   indexes and empties search_synonyms and listing_vectors (--production in production).
                   --quantization scalar builds the vector index with scalar quantization (default none).
  consent-report   read-only (P3.1). The number of users created on or after --since (a UTC date), and how
                   many of them lack a granted consent row for a required purpose, an age-declaration row,
                   or either, with the ids (at most 50) of those that lack one.
  facilitated-sales  read-only (P9.2). For an Indian financial year (--fy 2027-28: 1 April 2027 to 31 March 2028,
                   IST) it writes a CSV of shop id, shop name, completed pickups, completed deliveries and their
                   total value in rupees (app/services/leads.py facilitated_sales), for the CA's review of TDS on
                   e-commerce sales (business model risk R22). The file goes to --out, by default
                   ~/.nearshop/reports/facilitated-sales-FY<fy>.csv (outside the repository), and is never
                   overwritten unless --force is given. It prints only the file path and the row count.
  demand-audit     R5's audit of the "asked for near you" demand report (P9.7, app/services/demand_report.py).
                   status (read-only): whether reports are shown or held and why, NEARSHOP_DEMAND_REPORT_ENABLED,
                   and each audited week's labelled items and genuine share. label: shows the unlabelled audit
                   items one at a time (query, matched item, category, band and shop id) and records R5's label:
                   g genuine unmet demand, f search failure, j junk (s skips, q stops). failures (read-only): the
                   possible search failures, queries dropped because the shop itself has a matching in-stock listing
                   search did not find. build: builds this week's reports now, as the Monday job does (staging
                   checks). Nothing printed is a user id, a demand key, a time or a location.

Commands that write (backfill-thumbnails writes to the photo storage, not the database) show the
database name and ask the operator to type it; anything else changes nothing. No command prints an email
address or a password hash. Every command that changes a password or deactivates an account also ends that
account's sessions (a new token_version, no recorded sessions).
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import os
import secrets
import statistics
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path

from pydantic import EmailStr, TypeAdapter, ValidationError
from pymongo import UpdateOne
from pymongo.database import Database as MongoDatabase
from pymongo.errors import DuplicateKeyError, OperationFailure
from pymongo.operations import SearchIndexModel
from pymongo.results import BulkWriteResult

from app.ai import atlas_search
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.errors import AppError
from app.core.security import (
    encrypt_totp_secret,
    hash_password,
    hash_recovery_code,
    new_recovery_codes,
    new_totp_secret,
    provisioning_uri,
    revoke_all_update,
    totp_configured,
    verify_password,
)
from app.models import Role, User
from app.schemas.requests import AGE_PURPOSE, REQUIRED_PURPOSES
from app.services import leads, media
from app.utils.hours import IST
from database.seed.seed import RETIRED_DEMO_PASSWORD_SHA256

DEMO_DOMAIN = "nearshop.demo"  # admin@, priya@, owner@nearshop.demo and <shop>@shops.nearshop.demo
SYNTHETIC_DOMAIN = "example.com"  # the seed's generated customers; a reserved domain, so never a real inbox
ROTATED_PASSWORD_BYTES = 24  # token_urlsafe(24) is 32 characters
RETIRED_PASSWORD_ENV = "NEARSHOP_RETIRED_DEMO_PASSWORD"
MAX_ACTIVE_ADMINS = 3
THUMBNAIL_BATCH = 100
PHOTO_COLLECTIONS = ("shops", "products")  # the collections whose documents hold an image_path

Ask = Callable[[str], str]
Out = Callable[[str], None]


# ---------------------------------------------------------------- pure checks
def is_demo_email(email: str) -> bool:
    """True for addresses on the demo domain or one of its subdomains (x@nearshop.demo,
    x@shops.nearshop.demo), and not for look-alikes such as x@notnearshop.demo."""
    domain = email.strip().lower().rpartition("@")[2]
    return domain == DEMO_DOMAIN or domain.endswith("." + DEMO_DOMAIN)


def is_seed_generated(row: dict, last_demo_id: int | None) -> bool:
    """The seed's generated customers: on the reserved example.com domain and created in the seed run.
    The seed gives every user an id from one counter and creates the demo shop owners last, so an
    example.com account with a larger id than every demo account signed up after the seed and is
    counted with everyone else (created_at cannot tell them apart: the seed backdates it)."""
    email = row.get("email", "").strip().lower()
    return (email.endswith("@" + SYNTHETIC_DOMAIN) and last_demo_id is not None
            and isinstance(row["_id"], int) and row["_id"] < last_demo_id)


def is_retired_password(candidate: str) -> bool:
    """True when `candidate` is the demo password that backend/.env.example used to publish."""
    return hashlib.sha256(candidate.encode()).hexdigest() == RETIRED_DEMO_PASSWORD_SHA256


def uses_retired_password(password_hash: str, retired_password: str) -> bool:
    """The report's default-password check: does this stored Argon2 hash verify against the old
    public default? A local hash check only; it never signs in and never logs anything."""
    try:
        return verify_password(retired_password, password_hash)
    except Exception:  # an unreadable or foreign hash cannot match
        return False


def confirmed(db: Database, ask: Ask, out: Out) -> bool:
    """Ask the operator to type the database name; only an exact match allows a write."""
    name = db.mongo.name
    try:
        typed = ask(f"Type the database name ({name}) to confirm: ").strip()
    except EOFError:  # no input at all, e.g. stdin closed: treat as a refusal
        typed = ""
    if typed != name:
        out("Confirmation did not match the database name; nothing was changed.")
        return False
    return True


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "never"


def _ids(rows: list[dict]) -> str:
    return ", ".join(str(r["_id"]) for r in rows) or "none"


def _enrolled(row: dict) -> bool:
    return bool(row.get("totp_secret_enc"))


def _by_role(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(str(r.get("role", "?")), []).append(r)
    return dict(sorted(grouped.items()))


# ---------------------------------------------------------------- report
def report(db: Database, retired_password: str | None, out: Out = print) -> int:
    """Print the exposure report. Read-only. Never prints an email address or a hash."""
    rows = db.users.find_raw({}, {"email": 1, "role": 1, "is_active": 1, "last_login_at": 1, "password_hash": 1,
                                  "totp_secret_enc": 1}, sort=[("_id", 1)])
    demo = [r for r in rows if is_demo_email(r.get("email", ""))]
    demo_ids = {r["_id"] for r in demo}
    last_demo_id = max((i for i in demo_ids if isinstance(i, int)), default=None)
    synthetic, other = [], []
    for r in rows:
        if r["_id"] not in demo_ids:
            (synthetic if is_seed_generated(r, last_demo_id) else other).append(r)

    out(f"Database: {db.mongo.name}")
    by_role = Counter(r.get("role", "?") for r in rows)
    out(f"Users: {len(rows)} ({', '.join(f'{role} {n}' for role, n in sorted(by_role.items())) or 'none'})")

    out("")
    out(f"Accounts whose email does not end in {DEMO_DOMAIN}: {len(synthetic) + len(other)}")
    out(f"  Generated by the seed ({SYNTHETIC_DOMAIN}, created before the last demo account): {len(synthetic)}")
    for role, group in _by_role(synthetic).items():
        out(f"    role {role}: {len(group)}. Ids: {_ids(group)}")
    out(f"  Other, possibly real people: {len(other)}")
    for r in other:
        out(f"    id {r['_id']}  role {r.get('role')}  active {r.get('is_active', True)}")

    out("")
    admins = [r for r in rows if r.get("role") == Role.ADMIN.value]
    active = [r for r in admins if r.get("is_active", True)]
    enrolled = [r for r in active if _enrolled(r)]
    share = f"{round(100 * len(enrolled) / len(active))}%" if active else "n/a"
    out(f"Admins: {len(admins)} ({len(active)} active; second factor enrolled on {len(enrolled)} of {len(active)} "
        f"active, {share})")
    for r in admins:
        kind = "demo" if r["_id"] in demo_ids else "not demo"
        out(f"  id {r['_id']}  {kind}  active {r.get('is_active', True)}  TOTP {'enrolled' if _enrolled(r) else 'NOT enrolled'}"
            f"  last sign-in {_when(r.get('last_login_at'))}")

    out("")
    out(f"Demo accounts ({DEMO_DOMAIN}): {len(demo)}")
    if retired_password is None:
        out("  The old public default was not supplied, so the default-password check was skipped.")
    verdicts: dict[str, bool] = {}  # the seed gives many accounts the same hash; verify each hash once
    matching = 0
    for r in demo:
        line = f"  id {r['_id']}  {r.get('role')}  active {r.get('is_active', True)}  last sign-in {_when(r.get('last_login_at'))}"
        if retired_password is not None:
            stored = r.get("password_hash", "")
            if stored not in verdicts:
                verdicts[stored] = uses_retired_password(stored, retired_password)
            matching += verdicts[stored]
            line += f"  old default: {'MATCHES' if verdicts[stored] else 'no'}"
        out(line)

    if retired_password is not None:
        out("")
        if matching:
            out(f"RESULT: {matching} demo account(s) still accept the old public default. Run rotate-demo.")
        else:
            out("RESULT: no demo account matches the old public default.")
    return 0


# ---------------------------------------------------------------- demo-check
# Users on the demo domain or a subdomain of it (as is_demo_email), matched in the database, case-insensitively.
DEMO_EMAIL_PATTERN = r"@([^@]+\.)?" + DEMO_DOMAIN.replace(".", r"\.") + "$"


def demo_counts(mongo: MongoDatabase) -> dict[str, int]:
    """Synthetic data in a database: documents flagged is_demo, per collection (only those with any), and
    users on the demo domain. Read-only; views and system collections are skipped."""
    counts: dict[str, int] = {}
    for info in mongo.list_collections(filter={"type": "collection"}):
        name = info["name"]
        if name.startswith("system."):
            continue
        n = mongo[name].count_documents({"is_demo": True})
        if n:
            counts[name] = n
    return {"is_demo_documents": sum(counts.values()), **{f"is_demo.{k}": v for k, v in sorted(counts.items())},
            "demo_users": mongo["users"].count_documents({"email": {"$regex": DEMO_EMAIL_PATTERN, "$options": "i"}})}


def demo_check(db: Database, out: Out = print) -> int:
    """Print the synthetic-data counts; 0 when the database holds none, 1 otherwise. Read-only."""
    counts = demo_counts(db.mongo)
    out(f"Database: {db.mongo.name}")
    out(f"Documents flagged is_demo: {counts['is_demo_documents']}")
    for key, n in counts.items():
        if key.startswith("is_demo."):
            out(f"  {key.removeprefix('is_demo.')}: {n}")
    out(f"Users on {DEMO_DOMAIN}: {counts['demo_users']}")
    clean = counts["is_demo_documents"] == 0 and counts["demo_users"] == 0
    out("RESULT: no synthetic data." if clean else "RESULT: this database holds synthetic data.")
    return 0 if clean else 1


# ---------------------------------------------------------------- writes
class _AccountsChanged(Exception):
    def __init__(self, missing: int):
        super().__init__(missing)
        self.missing = missing


def rotate_demo(db: Database, ask: Ask = input, out: Out = print,
                new_password: Callable[[], str] = lambda: secrets.token_urlsafe(ROTATED_PASSWORD_BYTES)) -> int:
    """Give each demo account its own random password, after the typed confirmation."""
    rows = [r for r in db.users.find_raw({}, {"email": 1, "role": 1, "name": 1}, sort=[("_id", 1)])
            if is_demo_email(r.get("email", ""))]
    if not rows:
        out(f"No {DEMO_DOMAIN} accounts in database {db.mongo.name}; nothing to rotate.")
        return 0
    out(f"This sets a new random password on {len(rows)} {DEMO_DOMAIN} account(s) in database {db.mongo.name}.")
    if not confirmed(db, ask, out):
        return 1

    passwords = {r["_id"]: new_password() for r in rows}
    if any(len(p) < 24 for p in passwords.values()):
        raise SystemExit("Refusing to set a demo password shorter than 24 characters.")
    now = utcnow()
    # Each update matches only while the account still has the email that was read above, so an account
    # changed in between is never given a password meant for someone else.
    # A new password also ends every session of the account (revoke_all_update: a new token_version).
    ops = [UpdateOne({"_id": r["_id"], "email": r["email"]},
                     revoke_all_update({"password_hash": hash_password(passwords[r["_id"]]), "updated_at": now}))
           for r in rows]

    def write() -> None:
        result: BulkWriteResult = db.users.raw.bulk_write(ops, ordered=False, session=db.session)
        if result.matched_count != len(ops):  # raising aborts the transaction: nothing is changed
            raise _AccountsChanged(len(ops) - result.matched_count)

    try:
        db.transaction(write)  # all accounts change together or none do
    except _AccountsChanged as changed:
        out(f"{changed.missing} demo account(s) changed or were removed while rotating, so no password was "
            "changed. Run rotate-demo again.")
        return 1

    out("")
    out("New demo passwords. Store them in the team password manager now; they are not saved anywhere else.")
    out("Each line is the account id, role and name. To find the sign-in email, search the name on the "
        "admin Users page.")
    for r in rows:
        out(f"  id {r['_id']}  {r.get('role')}  {r.get('name', '')}  {passwords[r['_id']]}")
    out("")
    out("Every session these accounts had is ended: they must sign in again with the new passwords.")
    return 0


def deactivate_user(db: Database, user_id: int, ask: Ask = input, out: Out = print) -> int:
    user = db.users.get(user_id)
    if user is None:
        out(f"No user with id {user_id} in database {db.mongo.name}.")
        return 1
    if not user.is_active:
        out(f"User {user_id} ({user.role.value}) is already inactive; nothing to do.")
        return 0
    out(f"This deactivates user {user_id} ({user.role.value}) in database {db.mongo.name}. "
        "They can no longer sign in, and their current sessions stop working.")
    if not confirmed(db, ask, out):
        return 1
    db.users.update_one({"_id": user.id}, revoke_all_update({"is_active": False}))
    out(f"User {user_id} deactivated; every session of theirs has ended.")
    return 0


def list_admins(db: Database, out: Out = print) -> int:
    rows = db.users.find_raw({"role": Role.ADMIN.value}, {"created_at": 1, "is_active": 1, "totp_secret_enc": 1},
                             sort=[("_id", 1)])
    out(f"Admins in database {db.mongo.name}: {len(rows)}")
    for r in rows:
        out(f"  id {r['_id']}  created {_when(r.get('created_at'))}  active {r.get('is_active', True)}"
            f"  TOTP {'enrolled' if _enrolled(r) else 'NOT enrolled'}")
    return 0


def _valid_email(email: str) -> str | None:
    try:
        return TypeAdapter(EmailStr).validate_python(email.strip()).lower()
    except ValidationError:
        return None


def create_admin(db: Database, name: str, email: str, ask: Ask = input, out: Out = print,
                 new_password: Callable[[], str] = lambda: secrets.token_urlsafe(ROTATED_PASSWORD_BYTES)) -> int:
    """Create an admin account with its second factor enrolled, after the typed confirmation. Prints the
    password, the provisioning URI and the recovery codes once; stores the TOTP secret encrypted with
    NEARSHOP_TOTP_KEY and the recovery codes as digests. Refuses a fourth active admin."""
    if not totp_configured():
        out("NEARSHOP_TOTP_KEY is missing or invalid, so a second factor cannot be enrolled; no admin was created. "
            "Set it to the same value the API service has.")
        return 1
    name = name.strip()
    if len(name) < 2:
        out("Give the admin's name (two or more characters).")
        return 1
    address = _valid_email(email)
    if address is None:
        out("That is not a valid email address; no admin was created.")
        return 1
    active = db.users.find_raw({"role": Role.ADMIN.value, "is_active": True}, {"_id": 1}, sort=[("_id", 1)])
    if len(active) >= MAX_ACTIVE_ADMINS:
        out(f"Refusing to create a fourth active admin: database {db.mongo.name} already has {len(active)} "
            f"(ids {_ids(active)}). Deactivate one first with deactivate-user.")
        return 1
    if db.users.exists({"email": address}):
        out("An account with that email already exists; no admin was created.")
        return 1
    out(f"This creates admin account '{name}' in database {db.mongo.name}, with a second factor.")
    if not confirmed(db, ask, out):
        return 1

    password = new_password()
    if len(password) < 24:
        raise SystemExit("Refusing to set an admin password shorter than 24 characters.")
    secret, codes = new_totp_secret(), new_recovery_codes()

    def write() -> User | None:
        # Counted again inside the transaction, so the limit holds against a change since the check above.
        if db.users.count({"role": Role.ADMIN.value, "is_active": True}) >= MAX_ACTIVE_ADMINS:
            return None
        return db.users.insert(User(
            name=name, email=address, password_hash=hash_password(password), role=Role.ADMIN,
            totp_secret_enc=encrypt_totp_secret(secret), totp_enrolled_at=utcnow(),
            totp_recovery_hashes=[hash_recovery_code(c) for c in codes]))

    try:
        user = db.transaction(write)
    except DuplicateKeyError:
        out("An account with that email already exists; no admin was created.")
        return 1
    if user is None:
        out(f"Refusing to create a fourth active admin: database {db.mongo.name} reached {MAX_ACTIVE_ADMINS} "
            "active admins meanwhile.")
        return 1

    out("")
    out(f"Admin created: id {user.id}. The following is shown once and saved nowhere else; give it to the admin "
        "privately and store the password and recovery codes in the team password manager.")
    out(f"  Password: {password}")
    out("  Second factor: add this to an authenticator app (most apps also accept it as a QR code):")
    out(f"    {provisioning_uri(secret, f'admin-{user.id}')}")
    out("  Recovery codes, each usable once instead of an authenticator code:")
    for code in codes:
        out(f"    {code}")
    return 0


# ---------------------------------------------------------------- photo storage
def photo_keys(db: Database, batch_size: int = THUMBNAIL_BATCH):
    """Every stored photo key in the database, in batches of at most batch_size, read page by page by _id."""
    for name in PHOTO_COLLECTIONS:
        collection = getattr(db, name)
        last = None
        while True:
            query: dict = {"image_path": {"$nin": [None, ""]}}
            if last is not None:
                query["_id"] = {"$gt": last}
            rows = collection.find_raw(query, {"image_path": 1}, sort=[("_id", 1)], limit=batch_size)
            if not rows:
                break
            last = rows[-1]["_id"]
            yield [r["image_path"] for r in rows if isinstance(r.get("image_path"), str)]


def backfill_thumbnails(db: Database, ask: Ask = input, out: Out = print, batch_size: int = THUMBNAIL_BATCH,
                        storage: media.MediaStorage | None = None) -> int:
    """Create the missing thumbnail of every stored photo, after the typed confirmation. Idempotent: a photo
    whose thumbnail already exists is only measured. Returns 1 when a thumbnail could not be made."""
    if batch_size < 1:
        out("--batch-size must be 1 or more.")
        return 1
    storage = storage or media.get_storage()
    total = sum(len(batch) for batch in photo_keys(db, batch_size))
    out(f"Database {db.mongo.name} references {total} photo(s), stored on the {settings.media_backend} backend. "
        "This creates each missing thumbnail there; the database is not changed.")
    if not total:
        return 0
    if not confirmed(db, ask, out):
        return 1

    counts = Counter()
    sizes: list[int] = []
    seen: set[str] = set()
    for n, batch in enumerate(photo_keys(db, batch_size), start=1):
        for key in batch:
            if key in seen:
                continue
            seen.add(key)
            thumb_key = media.thumbnail_key(key)
            try:
                size = storage.size(thumb_key)
                if size is not None:
                    counts["existing"] += 1
                    sizes.append(size)
                    continue
                data = storage.get(key)
                if data is None:
                    counts["photo_missing"] += 1
                    continue
                thumb = media.make_thumbnail(data)
                storage.put(thumb_key, thumb, media.CONTENT_TYPE)
            except AppError:
                counts["unreadable"] += 1
                out(f"  could not read photo {key}; it has no thumbnail")
                continue
            except Exception as exc:
                counts["failed"] += 1
                out(f"  could not make the thumbnail of {key}: {type(exc).__name__}")
                continue
            counts["created"] += 1
            sizes.append(len(thumb))
        out(f"  batch {n}: {counts['created']} created, {counts['existing']} already there so far")

    lacking = counts["photo_missing"] + counts["unreadable"] + counts["failed"]
    median = statistics.median(sizes) if sizes else 0
    out("")
    out(f"Thumbnails: {counts['created']} created, {counts['existing']} already there.")
    out(f"Photos still without a thumbnail: {lacking} ({counts['photo_missing']} whose photo file is missing, "
        f"{counts['unreadable']} unreadable, {counts['failed']} failed; run again to retry failures).")
    out(f"Median thumbnail size: {median / 1024:.1f} KB over {len(sizes)} thumbnail(s).")
    return 1 if counts["unreadable"] or counts["failed"] else 0


# ---------------------------------------------------------------- Atlas Search indexes (P5.7)
PRODUCTION_WINDOW_IST = (time(21, 0), time(6, 0))  # create and drop on production only from 21:00 to 06:00 IST


SYSTEM_DATABASES = frozenset({"admin", "config", "local"})


class SearchIndexApi:
    """The search-index commands of one database, through PyMongo. Tests pass a fake with the same methods."""

    def __init__(self, mongo: MongoDatabase):
        self.mongo = mongo
        self.databases_listed = True  # False when the user may not list the cluster's databases (list())

    def list(self) -> list[dict]:
        """Every search and vector index in the cluster, as Atlas's limits count them (per cluster): database,
        collection, name, type, status and whether it is queryable. Every database the user can list is read; when
        it may list none, only this one (databases_listed is then False). Raises OperationFailure on a MongoDB
        without a search process."""
        try:
            names = [n for n in self.mongo.client.list_database_names() if n not in SYSTEM_DATABASES]
            self.databases_listed = True
        except OperationFailure:
            names, self.databases_listed = [], False
        out = []
        for db_name in sorted({*names, self.mongo.name}):
            mongo = self.mongo.client[db_name]
            for name in sorted(mongo.list_collection_names()):
                if name.startswith("system."):
                    continue
                for ix in mongo[name].list_search_indexes():
                    out.append({"database": db_name, "collection": name, "name": ix.get("name"),
                                "type": ix.get("type", "search"), "status": ix.get("status"),
                                "queryable": bool(ix.get("queryable"))})
        return out

    def create(self, spec: atlas_search.IndexSpec) -> None:
        self.mongo[spec.collection].create_search_index(
            SearchIndexModel(definition=spec.definition, name=spec.name, type=spec.type))

    def drop(self, collection: str, name: str) -> None:
        self.mongo[collection].drop_search_index(name)


def in_production_window(now: datetime) -> bool:
    """Whether a naive UTC time falls between 21:00 and 06:00 IST."""
    ist = now.replace(tzinfo=UTC).astimezone(IST).time()
    start, end = PRODUCTION_WINDOW_IST
    return ist >= start or ist < end


def _listed(api: SearchIndexApi, out: Out) -> list[dict] | None:
    """Every search and vector index in the cluster (SearchIndexApi.list), or None when they cannot be listed."""
    try:
        listed = api.list()
    except OperationFailure as exc:
        out(f"This MongoDB cannot list search indexes (it has no search process, or the user lacks the right): "
            f"{exc.details.get('errmsg') if exc.details else exc}")
        return None
    if not getattr(api, "databases_listed", True):
        out("WARNING: this user may not list the cluster's databases, so only this database's indexes are counted. "
            "Atlas counts the limit per cluster: check the other databases in the Atlas UI.")
    return listed


def _in_database(ix: dict, db_name: str) -> bool:
    return ix.get("database", db_name) == db_name


def cluster_limit(tier: str) -> int:
    """The most search and vector indexes create allows in the cluster: the tier's limit, and NearShop's 3."""
    return min(atlas_search.TIER_INDEX_LIMITS[tier], atlas_search.PLAN_MAX_INDEXES)


def search_index_plan(api: SearchIndexApi, tier: str | None, quantization: str = "none", out: Out = print) -> int:
    """Read-only: the definitions' sizes and synonym mappings, and the index count against the tier's limit."""
    plan = atlas_search.index_plan(quantization=quantization)
    out(f"Index plan ({len(plan)} indexes; NearShop allows {atlas_search.PLAN_MAX_INDEXES}):")
    for spec in plan:
        out(f"  {spec.name} on {spec.collection} ({spec.type}): definition {spec.size} bytes of "
            f"{atlas_search.MAX_DEFINITION_BYTES}, {spec.synonym_mappings} synonym mapping(s)")
    problems = atlas_search.plan_problems(plan)
    for p in problems:
        out(f"  PROBLEM: {p}")
    existing = _listed(api, out)
    if existing is not None:
        ours = {spec.name for spec in plan}
        others = [ix for ix in existing if not (ix["name"] in ours and _in_database(ix, api.mongo.name))]
        total = len(others) + len(plan)
        for t, limit in sorted(atlas_search.TIER_INDEX_LIMITS.items()):
            if tier in (None, t):
                verdict = "within" if total <= cluster_limit(t) else "OVER"
                out(f"After create: {total} search and vector indexes in the cluster ({len(others)} others), "
                    f"{verdict} {t}'s limit of {limit} and NearShop's {atlas_search.PLAN_MAX_INDEXES}.")
    return 1 if problems else 0


def search_index_status(api: SearchIndexApi, tier: str | None, out: Out = print) -> int:
    """Read-only: every search and vector index with its state, and the count against the tier's limit."""
    existing = _listed(api, out)
    if existing is None:
        return 1
    out(f"Search and vector indexes: {len(existing)}")
    for ix in existing:
        where = f"{ix['database']}.{ix['collection']}" if "database" in ix else ix["collection"]
        out(f"  {ix['name']} on {where} ({ix['type']}): {ix['status']}, "
            f"{'queryable' if ix['queryable'] else 'not queryable'}")
    over = False
    for t, limit in sorted(atlas_search.TIER_INDEX_LIMITS.items()):
        if tier in (None, t):
            over |= tier == t and len(existing) > limit
            out(f"{len(existing)} of {t}'s limit of {limit}{' (OVER)' if len(existing) > limit else ''}.")
    if len(existing) > atlas_search.PLAN_MAX_INDEXES:
        over |= tier is not None
        out(f"Above NearShop's {atlas_search.PLAN_MAX_INDEXES} (staging on M0 could not mirror this).")
    return 1 if over else 0


def _production_refusal(production_flag: bool, now: datetime) -> str | None:
    if settings.environment != "production":
        return None
    if not production_flag:
        return "This is production: add --production to change its search indexes."
    if not in_production_window(now):
        return "On production, search indexes change only from 21:00 to 06:00 IST."
    return None


def fill_listing_vectors(db: Database, out: Out) -> int | None:
    """listing_vectors for every active listing, reusing text_embeddings (embedding only texts it lacks). Returns the
    vectors' dimensions, which the vector index is built with, or None when nothing was written: the configured
    model did not load and the TF-IDF stand-in is in use, whose vectors are fitted in this process only."""
    from app.ai import semantic_index

    rows = db.products.find_raw({"is_active": True}, semantic_index.ROW_FIELDS, sort=[("_id", 1)])
    texts = [semantic_index._row_text(r) for r in rows]
    provider = semantic_index.get_provider(fallback_corpus=texts)
    if semantic_index.is_fallback(provider):
        out(f"listing_vectors: {settings.embedding_model} did not load on this machine (the TF-IDF stand-in is in "
            "use), so no vector was written. Fix the model download and run create again.")
        return None
    matrix = semantic_index.vectors(db, provider, texts)
    done = semantic_index.sync_listing_vectors(db, rows, texts, matrix, provider.name, full=True)
    out(f"listing_vectors: {len(rows)} active listings ({provider.name}, {provider.dim} dimensions), "
        f"{done['written']} written, {done['deleted']} deleted.")
    return provider.dim


def search_index_create(db: Database, api: SearchIndexApi, tier: str, quantization: str = "none",
                        production: bool = False, ask: Ask = input, out: Out = print,
                        now: datetime | None = None, fill: Callable[[Database, Out], int | None] | None = None) -> int:
    """Build the missing indexes of the plan, after the typed confirmation. Refuses: a plan that breaks the limits,
    more indexes in the cluster than the tier or NearShop (3) allows, listing_vectors that could not be filled, and
    production without --production or outside 21:00 to 06:00 IST. The vector index gets the dimensions of the
    vectors just written (fill)."""
    from app.services import synonyms

    if problem := _production_refusal(production, now or utcnow()):
        out(f"{problem} No index was created.")
        return 1
    if tier not in atlas_search.TIER_INDEX_LIMITS:
        out(f"--tier must be one of {', '.join(atlas_search.TIER_INDEX_LIMITS)}. No index was created.")
        return 1
    plan = atlas_search.index_plan(quantization=quantization)
    if problems := atlas_search.plan_problems(plan):
        out(f"The index plan breaks a limit ({'; '.join(problems)}). No index was created.")
        return 1
    existing = _listed(api, out)
    if existing is None:
        out("No index was created.")
        return 1
    names = {ix["name"] for ix in existing if _in_database(ix, db.mongo.name)}
    missing = [spec for spec in plan if spec.name not in names]
    after = len(existing) + len(missing)
    if after > cluster_limit(tier):
        tier_limit = atlas_search.TIER_INDEX_LIMITS[tier]
        which = (f"{tier}'s limit of {tier_limit}" if after > tier_limit else
                 f"NearShop's limit of {atlas_search.PLAN_MAX_INDEXES}, so staging on M0 can mirror production")
        out(f"Refusing: {len(existing)} search and vector indexes exist in the cluster and {len(missing)} more would "
            f"make {after}, above {which}. No index was created.")
        return 1
    if not missing:
        out("Every index of the plan exists already; nothing to create. Run status to see their state.")
        return 0
    out(f"This fills search_synonyms and listing_vectors and creates {len(missing)} index(es) "
        f"({', '.join(spec.name for spec in missing)}) in database {db.mongo.name} on {tier}.")
    if not confirmed(db, ask, out):
        return 1
    count = synonyms.regenerate_source(db, force=True)
    out(f"search_synonyms: {count} explicit mapping(s) from the active synonym entries.")
    if any(spec.collection == atlas_search.VECTOR_COLLECTION for spec in missing):
        dimensions = (fill or fill_listing_vectors)(db, out)
        if dimensions is None:
            out("No index was created.")
            return 1
        vector = next(spec for spec in atlas_search.index_plan(dimensions, quantization)
                      if spec.name == atlas_search.VECTOR_INDEX)
        missing = [vector if spec.name == vector.name else spec for spec in missing]
    for spec in missing:
        api.create(spec)
        out(f"  created {spec.name} on {spec.collection}; Atlas builds it in the background")
    out("Run search-index status until each is READY and queryable, and record its output in the release log.")
    return 0


def search_index_drop(db: Database, api: SearchIndexApi, production: bool = False, ask: Ask = input,
                      out: Out = print, now: datetime | None = None) -> int:
    """Remove the plan's indexes, and empty search_synonyms and listing_vectors, after the typed confirmation."""
    if problem := _production_refusal(production, now or utcnow()):
        out(f"{problem} Nothing was dropped.")
        return 1
    existing = _listed(api, out)
    if existing is None:
        out("Nothing was dropped.")
        return 1
    ours = {spec.name: spec.collection for spec in atlas_search.index_plan()}
    present = [ix for ix in existing if ix["name"] in ours and _in_database(ix, db.mongo.name)]
    out(f"This drops {len(present)} search index(es) and empties search_synonyms and listing_vectors in database "
        f"{db.mongo.name}.")
    if not confirmed(db, ask, out):
        return 1
    for ix in present:
        api.drop(ix["collection"], ix["name"])
        out(f"  dropped {ix['name']} on {ix['collection']}")
    for name in (atlas_search.SYNONYM_COLLECTION, atlas_search.VECTOR_COLLECTION):
        removed = db.mongo[name].delete_many({}).deleted_count
        out(f"  {name}: {removed} document(s) removed")
    return 0
# ---------------------------------------------------------------- consent report
CONSENT_REPORT_IDS = 50


def consent_report(db: Database, since: date, out: Out = print) -> int:
    """Users created on or after `since` (UTC) and how many lack a granted row for each required purpose or
    an age-declaration row (P3.1). Read-only; prints counts and ids, never an email address. Exits 1 when any
    user lacks a row. Admin accounts are made by create-admin, not by sign-up, so they never have consent rows:
    they are counted on a line of their own and left out of the check."""
    start = datetime(since.year, since.month, since.day)
    rows = db.users.find_raw({"created_at": {"$gte": start}}, {"_id": 1, "role": 1}, sort=[("_id", 1)])
    admins = sum(1 for r in rows if r.get("role") == Role.ADMIN.value)
    user_ids = [r["_id"] for r in rows if r.get("role") != Role.ADMIN.value]
    needed = (*REQUIRED_PURPOSES, AGE_PURPOSE)
    have: dict[int, set[str]] = {}
    for row in db.mongo["consents"].aggregate([
        {"$match": {"user_id": {"$in": user_ids}, "purpose": {"$in": list(needed)}, "granted": True}},
        {"$group": {"_id": "$user_id", "purposes": {"$addToSet": "$purpose"}}},
    ]):
        have[row["_id"]] = set(row["purposes"])
    no_required = [u for u in user_ids if not set(REQUIRED_PURPOSES) <= have.get(u, set())]
    no_age = [u for u in user_ids if AGE_PURPOSE not in have.get(u, set())]
    missing = sorted(set(no_required) | set(no_age))
    out(f"Consent report for database {db.mongo.name}, users created since {since.isoformat()} (UTC)")
    out(f"  users created by sign-up:             {len(user_ids)}")
    out(f"  admins created (not checked):         {admins}")
    out(f"  missing a required-purpose consent:   {len(no_required)}")
    out(f"  missing the age declaration:          {len(no_age)}")
    out(f"  missing either:                       {len(missing)}")
    complete = len(user_ids) - len(missing)
    out(f"  complete:                             {complete} ({complete / len(user_ids):.1%})" if user_ids
        else "  complete:                             no users in this period")
    if missing:
        shown = ", ".join(str(u) for u in missing[:CONSENT_REPORT_IDS])
        more = f" (and {len(missing) - CONSENT_REPORT_IDS} more)" if len(missing) > CONSENT_REPORT_IDS else ""
        out(f"  user ids missing a row: {shown}{more}")
    return 1 if missing else 0


# ---------------------------------------------------------------- facilitated sales (P9.2)
REPORTS_DIR = Path.home() / ".nearshop" / "reports"


def financial_year(value: str) -> int:
    """The starting year of an Indian financial year written 2027-28 (or 2027-2028, or 2027)."""
    head, _, tail = value.strip().partition("-")
    if not (head.isdigit() and len(head) == 4):
        raise argparse.ArgumentTypeError(f"{value!r} is not a financial year such as 2027-28")
    start = int(head)
    if tail and tail not in (f"{(start + 1) % 100:02d}", str(start + 1)):
        raise argparse.ArgumentTypeError(f"{value!r}: a financial year runs from April {start} to March {start + 1}")
    return start


def fy_label(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_text(value: str) -> str:
    """A text cell safe to open in a spreadsheet: text that a spreadsheet would run as a formula (it starts with =, +,
    -, @, a tab or a carriage return) gets a leading apostrophe, so shop owners' own names cannot inject formulas."""
    return f"'{value}" if value.startswith(CSV_FORMULA_START) else value


def facilitated_sales(db: Database, start_year: int, path: Path | None = None, *, force: bool = False,
                      out: Out = print) -> int:
    """Write the financial year's facilitated sales per shop as a CSV (module docstring). Read-only on the database;
    prints the path and the row count, never a shop's figures."""
    path = (path or REPORTS_DIR / f"facilitated-sales-FY{fy_label(start_year)}.csv").expanduser()
    if path.exists() and not force:
        out(f"{path} already exists; give --force to replace it, or another --out.")
        return 1
    rows = leads.facilitated_sales(db, start_year)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=leads.FACILITATED_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "shop_name": csv_text(row["shop_name"]), "total_value": f"{row['total_value']:.2f}"})
    out(f"{path}")
    out(f"{len(rows)} row(s)")
    return 0


# ---------------------------------------------------------------- demand report audit (P9.7)
AUDIT_KEYS = {"g": "genuine", "f": "search_failure", "j": "junk"}


def demand_audit_status(db: Database, out: Out = print) -> int:
    """Read-only: the report gate and every audited week."""
    from app.services import demand_report

    gate = demand_report.report_gate(db)
    out(f"Demand report in database {db.mongo.name}: NEARSHOP_DEMAND_REPORT_ENABLED is "
        f"{'on' if settings.demand_report_enabled else 'off'}.")
    out("Reports are SHOWN to owners." if gate["shown"] else f"Reports are HELD ({gate['code']}): {gate['reason']}")
    if gate["average_pct"] is not None:
        out(f"Last {settings.demand_report_audit_weeks} complete audits average {gate['average_pct']:g}% genuine "
            f"(needed: {settings.demand_report_min_precision_pct:g}%).")
    weeks = demand_report.audit_weeks(db)
    out(f"Audited weeks: {len(weeks)}")
    for w in weeks:
        share = f"{w['precision_pct']:g}% genuine" if w["complete"] else "incomplete"
        out(f"  {w['week']}: {w['labelled']} of {w['items']} labelled, {w['genuine']} genuine, {share}")
    out(f"Latest report week: {demand_report.latest_week(db) or 'none yet'}")
    return 0


def demand_audit_label(db: Database, ask: Ask = input, out: Out = print) -> int:
    """Show each unlabelled audit item and record R5's label, after the typed confirmation."""
    from app.services import demand_report

    if demand_report.next_unlabelled(db) is None:
        out("No unlabelled audit items. The weekly job draws the next sample on Monday.")
        return 0
    out(f"This records audit labels in database {db.mongo.name}.")
    if not confirmed(db, ask, out):
        return 1
    labelled, skipped = 0, set()
    while True:
        item = db.mongo[demand_report.AUDITS].find_one({"label": None, "_id": {"$nin": list(skipped)}},
                                                       sort=[("week", 1), ("_id", 1)])
        if item is None:
            break
        match = item.get("item_name") or "no catalogue item"
        out("")
        out(f"[{item['week']}] \"{item['query']}\"  shop {item['shop_id']}  matched: {match}  category: "
            f"{item.get('category') or 'none'}  people: {item['band']}")
        try:
            answer = ask("Label: g genuine, f search failure, j junk, s skip, q stop: ").strip().lower()
        except EOFError:
            answer = "q"
        if answer == "q":
            break
        if answer == "s":
            skipped.add(item["_id"])
            continue
        if answer not in AUDIT_KEYS:
            out("Type g, f, j, s or q.")
            continue
        if demand_report.record_label(db, item["_id"], AUDIT_KEYS[answer]):
            labelled += 1
    out(f"Labelled {labelled} item(s).")
    return 0


def demand_audit_failures(db: Database, out: Out = print) -> int:
    """Read-only: the possible search failures, newest week first."""
    from app.services import demand_report

    rows = demand_report.search_failures(db)
    out(f"Possible search failures: {len(rows)}{' (the newest 100)' if len(rows) == 100 else ''}")
    for r in rows:
        out(f"  [{r['week']}] shop {r['shop_id']}  \"{r['query']}\"  its matching listings: "
            f"{', '.join(str(i) for i in r['product_ids']) or 'none'}")
    return 0


def demand_audit_build(db: Database, ask: Ask = input, out: Out = print) -> int:
    """Build this week's reports now (as the Monday job does), after the typed confirmation. Prints counts only."""
    from app.services import demand_report

    out(f"This builds this week's demand reports in database {db.mongo.name}, replacing any built this week.")
    if not confirmed(db, ask, out):
        return 1
    result = demand_report.build_week(db)
    out(f"Week {result['week']}: {result['shops']} shop(s), {result['kept']} item(s) kept, "
        f"{result['search_failures']} possible search failure(s), {result['audit_sampled']} audit item(s) drawn.")
    out("Dropped: " + ", ".join(f"{k} {v}" for k, v in result["dropped"].items()))
    return 0


# ---------------------------------------------------------------- command line
def _retired_password_from_operator() -> str | None:
    """The old default, from the environment or hidden input, refused unless it matches the digest."""
    value = os.environ.get(RETIRED_PASSWORD_ENV)
    if value is None and sys.stdin.isatty():
        value = getpass.getpass("Old public demo password, to check the demo accounts against "
                                "(hidden; press Enter to skip the check): ")
    if not value:
        return None
    if not is_retired_password(value):
        raise SystemExit("That is not the old public demo password (its SHA-256 does not match the one in "
                         "database/seed/seed.py). Nothing was checked.")
    return value


def main(argv: list[str] | None = None, ask: Ask = input, out: Out = print) -> int:
    parser = argparse.ArgumentParser(prog="python -m database.admin_tasks", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", help="read-only exposure report; prints no email address")
    sub.add_parser("demo-check", help="read-only: count is_demo documents and nearshop.demo users")
    sub.add_parser("rotate-demo", help="new random password for every nearshop.demo account")
    deactivate = sub.add_parser("deactivate-user", help="stop one user from signing in")
    deactivate.add_argument("user_id", type=int)
    sub.add_parser("list-admins", help="ids, created dates and second-factor enrolment of admin accounts")
    create = sub.add_parser("create-admin", help="create an admin with a second factor (at most 3 active)")
    create.add_argument("--name", required=True)
    create.add_argument("--email", required=True)
    backfill = sub.add_parser("backfill-thumbnails", help="create missing photo thumbnails; safe to run again")
    backfill.add_argument("--batch-size", type=int, default=THUMBNAIL_BATCH)
    search_index = sub.add_parser("search-index", help="Atlas Search and Vector Search indexes (P5.7)")
    search_index.add_argument("action", choices=["plan", "create", "status", "drop"])
    search_index.add_argument("--tier", choices=sorted(atlas_search.TIER_INDEX_LIMITS),
                              help="the cluster's Atlas tier (required by create)")
    search_index.add_argument("--quantization", choices=atlas_search.QUANTIZATIONS, default="none",
                              help="the vector index's quantization (plan and create)")
    search_index.add_argument("--production", action="store_true",
                              help="required to create or drop on production (21:00 to 06:00 IST only)")
    consents = sub.add_parser("consent-report", help="read-only: users since a date without consent rows")
    consents.add_argument("--since", type=date.fromisoformat, required=True, help="a UTC date, YYYY-MM-DD")
    sales = sub.add_parser("facilitated-sales", help="read-only: a CSV of each shop's completed sales in a financial year")
    sales.add_argument("--fy", type=financial_year, required=True, help="the financial year, such as 2027-28")
    sales.add_argument("--out", type=Path, help="the CSV file (default ~/.nearshop/reports/...)")
    sales.add_argument("--force", action="store_true", help="replace the file if it exists")
    audit = sub.add_parser("demand-audit", help="R5's audit of the demand report (P9.7)")
    audit.add_argument("action", choices=["status", "label", "failures", "build"])
    args = parser.parse_args(argv)

    db = Database()
    if args.command == "search-index":
        api = SearchIndexApi(db.mongo)
        if args.action == "plan":
            return search_index_plan(api, args.tier, args.quantization, out)
        if args.action == "status":
            return search_index_status(api, args.tier, out)
        if args.action == "drop":
            return search_index_drop(db, api, args.production, ask, out)
        if args.tier is None:
            out("search-index create needs --tier (M0 or Flex).")
            return 1
        return search_index_create(db, api, args.tier, args.quantization, args.production, ask, out)
    if args.command == "report":
        return report(db, _retired_password_from_operator(), out)
    if args.command == "demo-check":
        return demo_check(db, out)
    if args.command == "rotate-demo":
        return rotate_demo(db, ask, out)
    if args.command == "deactivate-user":
        return deactivate_user(db, args.user_id, ask, out)
    if args.command == "create-admin":
        return create_admin(db, args.name, args.email, ask, out)
    if args.command == "backfill-thumbnails":
        return backfill_thumbnails(db, ask, out, args.batch_size)
    if args.command == "consent-report":
        return consent_report(db, args.since, out)
    if args.command == "demand-audit":
        actions = {"status": lambda: demand_audit_status(db, out), "label": lambda: demand_audit_label(db, ask, out),
                   "failures": lambda: demand_audit_failures(db, out), "build": lambda: demand_audit_build(db, ask, out)}
        return actions[args.action]()
    if args.command == "facilitated-sales":
        return facilitated_sales(db, args.fy, args.out, force=args.force, out=out)
    return list_admins(db, out)


if __name__ == "__main__":
    raise SystemExit(main())
