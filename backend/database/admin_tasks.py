"""Operator tools for the credential lockdown (plan step 0-D). Run from backend/:

    uv run python -m database.admin_tasks report
    uv run python -m database.admin_tasks rotate-demo
    uv run python -m database.admin_tasks deactivate-user 42
    uv run python -m database.admin_tasks list-admins

They act on the database in NEARSHOP_MONGODB_URI / NEARSHOP_MONGODB_DB, the same one the API uses,
without wiping anything (the seed is the tool that replaces a database).

  report           read-only. Database name, users by role, the ids and roles of accounts outside the
                   demo domain (the seed's generated customers apart from everyone else), last sign-in
                   of every admin and demo account, and whether each demo
                   account's stored hash still verifies against the old public demo password. That is a
                   local Argon2 check; nothing signs in. The old password is not kept in the repo (only
                   its SHA-256, in database/seed/seed.py), so the report asks for it with hidden input,
                   or reads NEARSHOP_RETIRED_DEMO_PASSWORD, and checks it against that digest first.
  rotate-demo      gives every account on the nearshop.demo domain (or a subdomain of it, such as
                   shops.nearshop.demo) its own random password and prints each one once, with the
                   account's id, role and name, for the team password manager.
  deactivate-user  sets is_active to false on one user id; the API rejects that user's sessions at once.
  list-admins      ids, created dates and status of admin accounts.

Commands that write show the database name and ask the operator to type it; anything else changes
nothing. No command prints an email address or a password hash.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import secrets
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from pymongo import UpdateOne
from pymongo.results import BulkWriteResult

from app.core.database import Database, utcnow
from app.core.security import hash_password, verify_password
from app.models import Role
from database.seed.seed import RETIRED_DEMO_PASSWORD_SHA256

DEMO_DOMAIN = "nearshop.demo"  # admin@, priya@, owner@nearshop.demo and <shop>@shops.nearshop.demo
SYNTHETIC_DOMAIN = "example.com"  # the seed's generated customers; a reserved domain, so never a real inbox
ROTATED_PASSWORD_BYTES = 24  # token_urlsafe(24) is 32 characters
RETIRED_PASSWORD_ENV = "NEARSHOP_RETIRED_DEMO_PASSWORD"

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


def _by_role(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(str(r.get("role", "?")), []).append(r)
    return dict(sorted(grouped.items()))


# ---------------------------------------------------------------- report
def report(db: Database, retired_password: str | None, out: Out = print) -> int:
    """Print the exposure report. Read-only. Never prints an email address or a hash."""
    rows = db.users.find_raw({}, {"email": 1, "role": 1, "is_active": 1, "last_login_at": 1, "password_hash": 1},
                            sort=[("_id", 1)])
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
    out(f"Admins: {len(admins)}")
    for r in admins:
        kind = "demo" if r["_id"] in demo_ids else "not demo"
        out(f"  id {r['_id']}  {kind}  active {r.get('is_active', True)}  last sign-in {_when(r.get('last_login_at'))}")

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
    ops = [UpdateOne({"_id": r["_id"], "email": r["email"]},
                     {"$set": {"password_hash": hash_password(passwords[r["_id"]]), "updated_at": now}})
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
    out("Sessions signed in before this keep working until they expire. To end them all now, change "
        "NEARSHOP_SECRET_KEY on the API service.")
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
    db.users.set(user, is_active=False)
    out(f"User {user_id} deactivated.")
    return 0


def list_admins(db: Database, out: Out = print) -> int:
    rows = db.users.find_raw({"role": Role.ADMIN.value}, {"created_at": 1, "is_active": 1}, sort=[("_id", 1)])
    out(f"Admins in database {db.mongo.name}: {len(rows)}")
    for r in rows:
        out(f"  id {r['_id']}  created {_when(r.get('created_at'))}  active {r.get('is_active', True)}")
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
    sub.add_parser("rotate-demo", help="new random password for every nearshop.demo account")
    deactivate = sub.add_parser("deactivate-user", help="stop one user from signing in")
    deactivate.add_argument("user_id", type=int)
    sub.add_parser("list-admins", help="ids and created dates of admin accounts")
    args = parser.parse_args(argv)

    db = Database()
    if args.command == "report":
        return report(db, _retired_password_from_operator(), out)
    if args.command == "rotate-demo":
        return rotate_demo(db, ask, out)
    if args.command == "deactivate-user":
        return deactivate_user(db, args.user_id, ask, out)
    return list_admins(db, out)


if __name__ == "__main__":
    raise SystemExit(main())
