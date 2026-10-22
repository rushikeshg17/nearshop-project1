import logging
import math
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, model_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.deps import DB, CurrentUser, OptionalUser
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.security import (
    TOTP_LOCK,
    TOTP_MAX_FAILURES,
    TotpNotConfigured,
    add_session_pipeline,
    create_access_token,
    decrypt_totp_secret,
    hash_password,
    hash_recovery_code,
    locked_until,
    looks_like_totp,
    match_totp,
    new_session,
    revoke_all_update,
    session_minutes,
    verify_password,
)
from app.models import Role, Shop, User
from app.schemas.requests import (
    CONSENT_PURPOSES,
    OPTIONAL_PURPOSES,
    OTP_CODE_PATTERN,
    PHONE_PATTERN,
    REQUIRED_PURPOSES,
    ConsentIn,
    LoginIn,
    OtpRequestIn,
    OtpVerifyIn,
    ProfileUpdateIn,
    RegisterIn,
    RegisterShopIn,
)
from app.schemas.serializers import iso
from app.services import otp
from app.services.loaders import categories
from app.services.meta import city_config
from app.services.privacy import (
    DeletionBlocked,
    consent_state,
    deletion_plan,
    export_data,
    nominee_out,
    remove_nominee,
    request_deletion,
    set_consent,
    set_nominee,
)
from app.utils.text import slugify

router = APIRouter(prefix="/auth", tags=["auth"])
# GET /api/me/export (P3.2): the data export, at the route the rate-limit table names (3 a day per user).
me_router = APIRouter(prefix="/me", tags=["auth"])
log = logging.getLogger("nearshop.auth")

EMAIL_TAKEN = Conflict("An account with this email already exists", code="email_taken")
PHONE_TAKEN = Conflict("An account with this phone number already exists. Sign in with a code sent to it instead.",
                       code="phone_taken")
# Where phone sign-in is unavailable, "sign in with a code" leads nowhere.
PHONE_TAKEN_NO_CODES = Conflict("An account with this phone number already exists. Sign in to that account "
                                "instead, or use another number.", code="phone_taken")
# A verify code for a number verified on another account: signing in to that account is not what was asked, and
# accounts are never merged automatically (P4.2): support looks at it.
PHONE_IN_USE = Conflict("This phone number is already verified on another NearShop account. We never merge accounts "
                        "automatically: to move the number to this account, contact us through the grievance page, "
                        "or use another number.", code="phone_taken")


def phone_taken() -> Conflict:
    return PHONE_TAKEN if otp.available() else PHONE_TAKEN_NO_CODES


def account_inactive() -> AppError:
    """A code proved the phone, but its account is switched off (by an admin). Said only to whoever holds the
    phone, so it tells nobody else anything."""
    return Forbidden("The account with this phone number is switched off. To ask about it, contact us through the "
                     "grievance page.", code="account_inactive")


# ---------------------------------------------------------------- the email sign-in sunset (P4.2)

# The roles that move to phone sign-in. Admins keep their password and authenticator code.
MIGRATING_ROLES = (Role.CUSTOMER, Role.OWNER)


def ist_today() -> date:
    """Today's IST calendar day (the sunset is an IST day), on the OTP clock that tests control."""
    return date.fromisoformat(otp.today())


def email_login_ended() -> bool:
    """Whether the sunset (settings.email_login_sunset) has come. Only where phone sign-in is available: without a
    provider nobody could verify a phone, so email sign-in carries on."""
    return otp.available() and ist_today() >= settings.email_login_sunset


def needs_verified_phone(user: User) -> bool:
    return user.role in MIGRATING_ROLES and user.phone_verified_at is None


def phone_migration(user: User) -> str | None:
    """What the site asks of this user (providers.tsx): "warn" shows the banner with the sunset date, "required" sends
    every page to /verify-phone, None asks nothing (a verified phone, an admin, or phone sign-in unavailable)."""
    if not needs_verified_phone(user) or not otp.available():
        return None
    return "required" if ist_today() >= settings.email_login_sunset else "warn"


def email_login_closed() -> AppError:
    return Forbidden("Email sign-in has ended for customer and shop accounts. Sign in with your phone number instead. "
                     "If you never verified a phone number on this account, contact us through the grievance page.",
                     code="phone_signin_required")


def invalid_credentials() -> AppError:
    return AppError("Email or password is incorrect", code="invalid_credentials", status_code=401)


def password_ok(password: str, password_hash: str) -> bool:
    """Whether password matches. An account without a password (registered by phone, or deleted) has an empty
    hash, which matches nothing."""
    return bool(password_hash) and verify_password(password, password_hash)


def _phone_key(exc: DuplicateKeyError) -> bool:
    return "phone" in ((exc.details or {}).get("keyPattern") or {})


def _taken(exc: DuplicateKeyError) -> AppError:
    """The 409 for a unique-index refusal on users: uq_phone, else uq_email_set."""
    return phone_taken() if _phone_key(exc) else EMAIL_TAKEN


class LoginWithCodeIn(LoginIn):
    """The sign-in body. code is the admin second factor: a 6-digit authenticator code or a recovery code.
    Other roles leave it out."""

    code: str | None = Field(default=None, min_length=1, max_length=32)


class PhoneVerifyIn(OtpVerifyIn):
    """POST /api/auth/otp/verify. A phone registration may also give an email (P4.2: optional), kept as a contact
    address; the account has no password, so it never signs in with it."""

    email: EmailStr | None = None

    @model_validator(mode="after")
    def _email_only_to_register(self) -> "PhoneVerifyIn":
        if self.email is not None and self.purpose != "register":
            raise ValueError("An email is given only when creating an account")
        return self


class OwnerSignupIn(RegisterIn):
    """The owner of a shop sign-up. code: the 6-digit code sent to the owner's phone (purpose register), which
    verifies it (P4.2); the screens send it whenever phone sign-in is available."""

    code: str | None = Field(default=None, pattern=OTP_CODE_PATTERN)


class RegisterShopWithCodeIn(RegisterShopIn):
    owner: OwnerSignupIn


class PasswordChangeIn(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class NomineeIn(BaseModel):
    """One nominee (plan section 8.5): a name, and a phone or an email (or both)."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _phone_or_email(self) -> "NomineeIn":
        if not self.phone and not self.email:
            raise ValueError("Give the nominee's phone number or email address")
        return self


class DeleteAccountIn(BaseModel):
    """Deleting the account: the current password, or a code sent to the account's verified phone (purpose
    delete; an account registered by phone has no password), and whether to cancel open reservations and orders
    first."""

    model_config = {"extra": "forbid"}

    password: str | None = Field(default=None, min_length=1, max_length=128)
    code: str | None = Field(default=None, pattern=OTP_CODE_PATTERN)
    cancel_open: bool = False

    @model_validator(mode="after")
    def _password_or_code(self) -> "DeleteAccountIn":
        if not self.password and not self.code:
            raise ValueError("Give your password, or a code sent to your phone")
        return self


OptionalPurpose = Literal[OPTIONAL_PURPOSES]  # one consent endpoint per optional purpose


def user_out(user: User, db: Database) -> dict:
    shop = None
    if user.role == Role.OWNER:
        s = db.shops.find_one({"owner_id": user.id}, sort=[("_id", 1)])
        if s:
            shop = {"id": s.id, "slug": s.slug, "name": s.name}
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        # P4.2: whether the phone is verified, the email sign-in sunset (an IST day) and what the site asks.
        "phone_verified": user.phone_verified_at is not None,
        "email_login_sunset": settings.email_login_sunset.isoformat(),
        "phone_migration": phone_migration(user),
        "role": user.role.value,
        "home_lat": user.home_lat,
        "home_lng": user.home_lng,
        "home_label": user.home_label,
        "shop": shop,
    }


def _end_cookie(response: Response) -> None:
    response.delete_cookie(settings.cookie_name, path="/")


def _start_session(request: Request, response: Response, db: Database, user: User,
                   extra: dict | None = None) -> User:
    """Record a new session for this device (dropping expired ones and, past 10, the oldest) and set its
    cookie. `extra` fields are written in the same update. Returns the user as stored afterwards."""
    role = user.role.value
    session = new_session(request.headers.get("user-agent"), role)
    doc = db.users.raw.find_one_and_update({"_id": user.id}, add_session_pipeline(session, extra),
                                           return_document=ReturnDocument.AFTER, session=db.session)
    if doc is None:  # the account was removed in between
        raise invalid_credentials()
    token = create_access_token(user.id, role, version=doc["token_version"], session_id=session["id"])
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=session_minutes(role) * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return User.model_validate(doc)


def _revoke_all(request: Request, response: Response, db: Database, user: User, extra: dict | None = None,
                *, continue_here: bool) -> User:
    """End every session of `user` (a new token_version, no sessions), writing `extra` in the same update.
    With continue_here a fresh session is started for this device, except for admins, who sign in again
    with their second factor."""
    db.users.update_one({"_id": user.id}, revoke_all_update(extra))
    for key, value in (extra or {}).items():
        setattr(user, key, value)
    if continue_here and user.role != Role.ADMIN:
        return _start_session(request, response, db, user)
    _end_cookie(response)
    return user


def _out_after_revoke(user: User, db: Database) -> dict:
    """user_out, flagged signed_out for an admin, whose sessions all ended (no new one without the code)."""
    return {**user_out(user, db), "signed_out": True} if user.role == Role.ADMIN else user_out(user, db)


def require_open_signups() -> None:
    """Close both sign-up endpoints until launch (settings.signups_open). FastAPI resolves route
    dependencies before it validates the body, so a closed endpoint answers 403 to any payload."""
    if not settings.signups_open:
        raise Forbidden("Sign-ups are paused while we prepare for launch.", code="signups_closed")


def _ensure_email_free(db: Database, email: str) -> None:
    if db.users.exists({"email": email.lower()}):
        raise EMAIL_TAKEN


def _phone_to_keep(db: Database, phone: str | None) -> str | None:
    """The phone an email registration keeps (one account per phone, uq_phone; it stays unverified). 409 when an
    active account has verified it: that account is the number's, and signing in to it is the way on. When
    another account only typed it in (never verified), or its account is switched off, the registration goes
    ahead without it (None): a number typed in proves nothing, so it may not lock its owner out, and its owner
    takes it with a verify code, which releases it (_release_unverified)."""
    if not phone:
        return None
    holder = db.users.find_one({"phone": phone})
    if holder is None:
        return phone
    if holder.phone_verified_at is not None and holder.is_active:
        raise phone_taken()
    return None


def _with_phone_notice(out: dict, asked: str | None, kept: str | None) -> dict:
    """user_out, with phone_not_saved when the number given could not be kept (_phone_to_keep)."""
    return {**out, "phone_not_saved": True} if asked and not kept else out


def check_consent(consent: ConsentIn) -> None:
    """Registration needs the required purposes ticked (422 consent_required) and the 18-or-older
    declaration (422 age_confirmation_required). Children's data would need verifiable parental consent
    (DPDP Act section 9), which NearShop does not collect."""
    if not all(getattr(consent, purpose) for purpose in REQUIRED_PURPOSES):
        raise AppError("Please agree to the privacy notice and terms to create an account.",
                       code="consent_required", status_code=422)
    if not consent.age_18_plus:
        raise AppError("NearShop is for adults. You need to be 18 or older to create an account.",
                       code="age_confirmation_required", status_code=422)


def record_consents(db: Database, user_id: int, consent: ConsentIn, *, source: str = "web") -> None:
    """Append one consents row per purpose and one for the age declaration, against the current notice
    version. Rows are never updated or deleted here: a later change of mind is written as a new row, and
    the newest row for a purpose is the one in force. Joins the caller's transaction, if any."""
    at = utcnow()
    rows = [{"_id": row_id, "user_id": user_id, "purpose": purpose, "notice_version": settings.notice_version,
             "granted": bool(getattr(consent, purpose)), "at": at, "source": source}
            for row_id, purpose in zip(db.next_ids("consents", len(CONSENT_PURPOSES)), CONSENT_PURPOSES)]
    db.mongo["consents"].insert_many(rows, session=db.session)


@router.post("/register", dependencies=[Depends(require_open_signups)])
def register(body: RegisterIn, request: Request, response: Response, db: DB):
    check_consent(body.consent)
    _ensure_email_free(db, body.email)
    phone = _phone_to_keep(db, body.phone)

    def create() -> User:
        # The account and its consent rows are written together or not at all.
        user = db.users.insert(User(name=body.name, email=body.email.lower(), phone=phone,
                                    password_hash=password_hash, role=Role.CUSTOMER))
        record_consents(db, user.id, body.consent)
        return user

    password_hash = hash_password(body.password)
    try:
        user = db.transaction(create)
    except DuplicateKeyError as exc:  # two sign-ups raced: the unique indexes on email and phone decide
        raise _taken(exc) from None
    user = _start_session(request, response, db, user)
    return _with_phone_notice(user_out(user, db), body.phone, phone)


def _verifiable_phone(db: Database, phone: str) -> None:
    """Before a sign-up spends a code for `phone`: 409 when an account has already verified it (403 when that
    account is switched off). One that only typed it in loses it once the code proves the phone."""
    holder = _verified_holder(db, phone)
    if holder is not None:
        raise PHONE_TAKEN if holder.is_active else account_inactive()


@router.post("/register-shop", dependencies=[Depends(require_open_signups)])
def register_shop(body: RegisterShopWithCodeIn, request: Request, response: Response, db: DB):
    """A shop and its owner's account. With owner.code (a register code sent to the owner's phone, which is the
    shop's unless the owner gives another), the owner's phone is verified (P4.2), and so is the shop's when it is
    the same number, which P3.3 needs before the shop goes live. Without one the phone stays unverified (where
    phone sign-in is unavailable): an admin records the call-back instead (P3.3)."""
    check_consent(body.owner.consent)
    _ensure_email_free(db, body.owner.email)
    code_phone = body.owner.phone or body.shop.phone
    verified_at = None
    if body.owner.code:
        if not otp.available():
            raise otp.unavailable()
        _verifiable_phone(db, code_phone)
        kept, owner_phone = code_phone, code_phone
    else:
        kept = _phone_to_keep(db, body.owner.phone)
        # The owner's phone defaults to the shop's, unless another account already has that number (one per phone).
        owner_phone = kept or (None if body.owner.phone or db.users.exists({"phone": body.shop.phone})
                               else body.shop.phone)
    cats = [c for c in categories(db).values() if c.slug in set(body.shop.category_slugs)]
    if len(cats) != len(set(body.shop.category_slugs)):
        raise AppError("Choose categories from the list")
    password_hash = hash_password(body.owner.password)
    data = body.shop.model_dump(exclude={"category_slugs"})
    base = slugify(f"{body.shop.name} {body.shop.locality or ''}")
    taken = {d["slug"] for d in db.shops.find_raw({"slug": {"$regex": f"^{base}(-\\d+)?$"}}, {"slug": 1})}
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    if body.owner.code:  # last, so that a sign-up refused above has not spent the code
        otp.verify_code(db, code_phone, "register", body.owner.code)
        verified_at = utcnow()
    shop_verified_at = verified_at if body.shop.phone == owner_phone else None

    def create() -> User:
        # The owner account and the shop are created together or not at all.
        if verified_at is not None:
            _release_unverified(db, code_phone)
        user = db.users.insert(User(name=body.owner.name, email=body.owner.email.lower(), phone=owner_phone,
                                    phone_verified_at=verified_at, password_hash=password_hash, role=Role.OWNER))
        db.shops.insert(Shop(**data, owner_id=user.id, slug=slug, city=city_config()["name"],
                             category_ids=[c.id for c in cats], inventory_updated_at=utcnow(),
                             phone_verified_at=shop_verified_at))
        record_consents(db, user.id, body.owner.consent)
        return user

    try:
        user = db.transaction(create)
    except DuplicateKeyError as exc:
        raise _taken(exc) from None
    user = _start_session(request, response, db, user)
    return _with_phone_notice(user_out(user, db), body.owner.phone, kept)


def _lock_message(until: datetime) -> str:
    minutes = max(1, math.ceil((until - utcnow()).total_seconds() / 60))
    return (f"Too many wrong codes. Admin sign-in for this account is locked for {minutes} more "
            f"minute{'s' if minutes != 1 else ''}.")


def _check_second_factor(db: Database, user: User, code: str | None) -> None:
    """Admins confirm the password with a TOTP code or a single-use recovery code. TOTP_MAX_FAILURES wrong
    codes in a row lock admin sign-in for the account for TOTP_LOCK. A TOTP code is accepted once only (its
    time step must be later than the last one accepted), and a recovery code is removed as it is used; both
    are single atomic updates, so two requests cannot spend the same code."""
    if not user.totp_enrolled:
        raise Forbidden("This admin account has no second factor. Admin accounts are created with the "
                        "admin_tasks create-admin command, which enrols one.", code="totp_not_enrolled")
    now = utcnow()
    if until := locked_until(user.totp_locked_until, now):
        raise AppError(_lock_message(until), code="totp_locked", status_code=429)
    if not code:
        raise Forbidden("Enter the 6-digit code from your authenticator app, or a recovery code.",
                        code="totp_required")
    try:
        secret = decrypt_totp_secret(user.totp_secret_enc or "")
    except TotpNotConfigured as exc:
        log.error("admin second factor unavailable: %s", exc)
        raise AppError("Admin sign-in is not available right now.", code="totp_unavailable",
                       status_code=503) from None

    if looks_like_totp(code):
        step = match_totp(secret, code)
        ok = step is not None and db.users.update_one(
            {"_id": user.id, "$or": [{"totp_last_step": {"$lt": step}}, {"totp_last_step": {"$exists": False}}]},
            {"$set": {"totp_last_step": step, "totp_failures": 0}}) == 1
    else:
        digest = hash_recovery_code(code)
        ok = db.users.update_one({"_id": user.id, "totp_recovery_hashes": digest},
                                 {"$pull": {"totp_recovery_hashes": digest}, "$set": {"totp_failures": 0}}) == 1
        if ok:
            log.warning("admin %s signed in with a recovery code", user.id)
    if ok:
        return

    doc = db.users.raw.find_one_and_update({"_id": user.id}, {"$inc": {"totp_failures": 1}},
                                           return_document=ReturnDocument.AFTER, session=db.session)
    if doc and doc.get("totp_failures", 0) >= TOTP_MAX_FAILURES:
        until = now + TOTP_LOCK
        db.users.update_one({"_id": user.id}, {"$set": {"totp_locked_until": until, "totp_failures": 0}})
        log.warning("admin %s sign-in locked after %d wrong codes", user.id, TOTP_MAX_FAILURES)
        raise AppError(_lock_message(until), code="totp_locked", status_code=429)
    raise AppError("That code is not right. Check your authenticator app and try again.", code="invalid_code",
                   status_code=401)


@router.post("/login")
def login(body: LoginWithCodeIn, request: Request, response: Response, db: DB):
    user = db.users.find_one({"email": body.email.lower()})
    # Same message for unknown email and wrong password, so accounts cannot be enumerated.
    if not user or not password_ok(body.password, user.password_hash) or not user.is_active:
        raise invalid_credentials()
    # After the sunset (P4.2), only the phone signs customers and owners in until they verify one. Checked after
    # the password, so the answer says nothing about an account to someone without it.
    if needs_verified_phone(user) and email_login_ended():
        raise email_login_closed()
    if user.role == Role.ADMIN:
        _check_second_factor(db, user, body.code)
    user = _start_session(request, response, db, user, {"last_login_at": utcnow()})
    return user_out(user, db)


@router.post("/logout")
def logout(request: Request, response: Response, user: OptionalUser, db: DB):
    """Ends this device's session only: its token is refused from the next request, even a copy of it."""
    sid = getattr(request.state, "session_id", None)
    if user is not None and sid:
        db.users.update_one({"_id": user.id}, {"$pull": {"sessions": {"id": sid}}})
    _end_cookie(response)
    return {"ok": True}


@router.post("/logout-everywhere")
def logout_everywhere(request: Request, response: Response, user: CurrentUser, db: DB):
    """Sign out everywhere: every session of this account ends, this one included."""
    _revoke_all(request, response, db, user, continue_here=False)
    return {"ok": True}


@router.get("/sessions")
def sessions(request: Request, user: CurrentUser):
    """This account's signed-in devices, most recently used first."""
    current = getattr(request.state, "session_id", None)
    now = utcnow()
    rows = sorted((s for s in user.sessions if s.expires_at > now), key=lambda s: s.last_seen_at, reverse=True)
    return [{"id": s.id, "device": s.device, "created_at": iso(s.created_at), "last_seen_at": iso(s.last_seen_at),
             "current": s.id == current} for s in rows]


@router.delete("/sessions/{session_id}")
def revoke_session(session_id: str, request: Request, response: Response, user: CurrentUser, db: DB):
    """Sign out one device. Revoking the current session also clears this browser's cookie."""
    if db.users.update_one({"_id": user.id}, {"$pull": {"sessions": {"id": session_id}}}) != 1:
        raise NotFound("That session has already ended")
    if session_id == getattr(request.state, "session_id", None):
        _end_cookie(response)
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordChangeIn, request: Request, response: Response, user: CurrentUser, db: DB):
    """Change the password. Every session ends; this device gets a new one (admins sign in again)."""
    if not password_ok(body.current_password, user.password_hash):
        raise AppError("Your current password is not right", code="wrong_password")
    user = _revoke_all(request, response, db, user, {"password_hash": hash_password(body.new_password)},
                       continue_here=True)
    return _out_after_revoke(user, db)


@router.get("/me")
def me(user: OptionalUser, db: DB):
    """The signed-in user, or null (200) when signed out so browsers don't log errors."""
    return user_out(user, db) if user else None


@router.patch("/me")
def update_me(body: ProfileUpdateIn, user: CurrentUser, db: DB):
    """Name and home location. A phone is refused here (422): it changes only through a verify code (P4.1)."""
    changes = body.model_dump(exclude_unset=True)
    if changes:
        db.users.set(user, **changes)
    return user_out(user, db)


# ---------------------------------------------------------------- phone codes (P4.1, app/services/otp.py)


@router.get("/phone-signin")
def phone_signin():
    """Whether phone sign-in is available here (a provider configured); P4.2 shows the phone screens only then. Also
    the email sign-in sunset (an IST day) and whether it has come (email_login_ended), for the sign-in screen."""
    return {**otp.status(), "email_login_sunset": settings.email_login_sunset.isoformat(),
            "email_login_ended": email_login_ended()}


def _otp_preconditions(purpose: str, user: User | None) -> int | None:
    """What a purpose needs before any code is sent or checked, the same for every phone: phone sign-in available,
    sign-ups open to register, a signed-in user to verify or delete (whose id the code is then bound to)."""
    if not otp.available():
        raise otp.unavailable()
    if purpose == "register":
        require_open_signups()
    if purpose in ("verify", "delete"):
        if user is None:
            raise AppError("Please sign in first", code="unauthorized", status_code=401)
        return user.id
    return None


def _has_verified_phone(user: User, phone: str | None = None) -> bool:
    return bool(user.phone) and user.phone_verified_at is not None and (phone is None or phone == user.phone)


def _no_verified_phone() -> AppError:
    return AppError("Your account has no verified phone number to send a code to. Use your password instead.",
                    code="phone_not_verified")


@router.post("/otp/request")
def request_otp(body: OtpRequestIn, user: OptionalUser, db: DB):
    """Send a 6-digit code to the phone (purpose login, register, verify, reset or delete). The answer is the same
    whether or not an account uses the phone (a delete code goes only to the signed-in user's own verified phone);
    channel picks the channel to try first. Limits: app/core/ratelimit.py and the daily cap in app/services/otp.py."""
    user_id = _otp_preconditions(body.purpose, user)
    if body.purpose == "delete" and not _has_verified_phone(user, body.phone):
        raise _no_verified_phone()
    return otp.request_code(db, body.phone, body.purpose, user_id=user_id, channel=body.channel)


def _no_account() -> AppError:
    if email_login_ended():
        return NotFound("No account has this verified phone number. Create an account with it, or, if you used to "
                        "sign in with email, contact us through the grievance page.", code="no_account")
    return NotFound("No account has this verified phone number yet. Create an account, or sign in with your "
                    "email and verify your phone on the verify-phone page.", code="no_account")


def _verified_holder(db: Database, phone: str) -> User | None:
    """The account whose verified phone this is, active or switched off (the purposes answer account_inactive
    for one switched off). Phone sign-in never reaches an account whose phone was only typed in (never
    verified): that number may not be its owner's."""
    holder = db.users.find_one({"phone": phone})
    return holder if holder and holder.phone_verified_at is not None else None


def _release_unverified(db: Database, phone: str, keep: int | None = None) -> None:
    """A code just proved who holds the phone, so another account that only typed it in (unverified) loses it."""
    released = db.users.update_one({"phone": phone, "phone_verified_at": None, "_id": {"$ne": keep}},
                                   {"$set": {"phone": None, "updated_at": utcnow()}})
    if released:
        log.info("an unverified phone was released from another account by a verified code")


@router.post("/otp/verify")
def verify_otp(body: PhoneVerifyIn, request: Request, response: Response, user: OptionalUser, db: DB):
    """Check the code, then act on its purpose:
      * login: start a session on the account with this verified phone (the same cookie as a password sign-in).
        Admins sign in with their password and second factor only.
      * register: a customer account for the phone, with the P3.1 consent (checked before the code is spent) and,
        optionally, an email (P4.2).
      * verify: set (or confirm) the signed-in user's phone. A new number ends every session (P2.4); this device
        continues on a new one, and an admin signs in again.
      * reset: a new password for the account with this verified phone; every session ends, this device signs in.
    """
    user_id = _otp_preconditions(body.purpose, user)
    if body.purpose == "register":
        check_consent(body.consent)
        if body.email:
            _ensure_email_free(db, body.email)
    otp.verify_code(db, body.phone, body.purpose, body.code, user_id=user_id)
    holder = _verified_holder(db, body.phone)
    now = utcnow()

    if body.purpose in ("login", "reset", "register") and holder is not None and not holder.is_active:
        raise account_inactive()

    if body.purpose == "login":
        if holder is None:
            raise _no_account()
        if holder.role == Role.ADMIN:
            raise Forbidden("Admins sign in with their email, password and authenticator code.",
                            code="password_required")
        holder = _start_session(request, response, db, holder, {"last_login_at": now})
        return user_out(holder, db)

    if body.purpose == "reset":
        if holder is None:
            raise _no_account()
        if holder.role == Role.ADMIN:
            raise Forbidden("Admin passwords are reset by another admin with the operator command line.",
                            code="admin_reset")
        holder = _revoke_all(request, response, db, holder, {"password_hash": hash_password(body.new_password)},
                             continue_here=True)
        return user_out(holder, db)

    if body.purpose == "register":
        if holder is not None:
            raise PHONE_TAKEN

        def create() -> User:
            _release_unverified(db, body.phone)
            new = db.users.insert(User(name=body.name, email=body.email.lower() if body.email else None,
                                       phone=body.phone, phone_verified_at=now, password_hash="", role=Role.CUSTOMER))
            record_consents(db, new.id, body.consent)
            return new

        try:
            new_user = db.transaction(create)
        except DuplicateKeyError as exc:
            raise (PHONE_TAKEN if _phone_key(exc) else EMAIL_TAKEN) from None
        new_user = _start_session(request, response, db, new_user, {"last_login_at": now})
        return user_out(new_user, db)

    # verify: the signed-in user's phone (_otp_preconditions refused a verify code without one)
    if user is None:
        raise _no_account()
    if holder is not None and holder.id != user.id:
        raise PHONE_IN_USE
    if body.phone == user.phone:
        db.users.set(user, phone_verified_at=now)
        return user_out(user, db)
    _release_unverified(db, body.phone, keep=user.id)
    try:
        user = _revoke_all(request, response, db, user, {"phone": body.phone, "phone_verified_at": now},
                           continue_here=True)
    except DuplicateKeyError:
        raise PHONE_IN_USE from None
    return _out_after_revoke(user, db)


# ---------------------------------------------------------------- privacy rights (P3.2, app/services/privacy.py)


@router.get("/privacy")
def privacy_settings(user: CurrentUser, db: DB):
    """What the account page's privacy section shows: consents in force, the nominee and the deletion outcome."""
    raw = db.users.raw.find_one({"_id": user.id}, {"nominee": 1}) or {}
    return {"consents": consent_state(db, user.id), "optional_purposes": list(OPTIONAL_PURPOSES),
            "nominee": nominee_out(raw.get("nominee")), "deletion": deletion_plan(db, user)}


@router.put("/consents/{purpose}")
def grant_consent(purpose: OptionalPurpose, user: CurrentUser, db: DB):
    """Turn an optional purpose on: a new consents row, granted, against the current notice version."""
    return {"consents": set_consent(db, user.id, purpose, True)}


@router.delete("/consents/{purpose}")
def withdraw_consent(purpose: OptionalPurpose, user: CurrentUser, db: DB):
    """Withdraw an optional purpose: a new consents row, not granted. Takes effect on the next request."""
    return {"consents": set_consent(db, user.id, purpose, False)}


@router.put("/nominee")
def put_nominee(body: NomineeIn, user: CurrentUser, db: DB):
    """Name the account's nominee, replacing any earlier one."""
    return {"nominee": set_nominee(db, user, body.name, body.phone, body.email)}


@router.delete("/nominee")
def delete_nominee(user: CurrentUser, db: DB):
    remove_nominee(db, user)
    return {"nominee": None}


@router.get("/deletion")
def deletion_preview(user: CurrentUser, db: DB):
    """What deleting the account would do now (outcome, open work, the date for a scheduled deletion)."""
    return deletion_plan(db, user)


@router.post("/delete")
def delete_account(body: DeleteAccountIn, response: Response, user: CurrentUser, db: DB):
    """Delete the account (plan section 8.5): at once, or scheduled while orders are in flight. Confirmed with the
    password, or with a code sent to the account's verified phone (POST /api/auth/otp/request, purpose delete),
    which is how an account registered by phone (no password) confirms it. 409 deletion_blocked lists open
    reservations and orders unless cancel_open is set; admins get 403."""
    if user.role == Role.ADMIN:
        raise Forbidden("Admin accounts cannot delete themselves. Another admin removes the account with the "
                        "operator command line.", code="admin_self_delete")
    if body.password is not None:
        if not password_ok(body.password, user.password_hash):
            raise AppError("Your password is not right", code="wrong_password")
    else:
        if not otp.available():
            raise otp.unavailable()
        if not _has_verified_phone(user):
            raise _no_verified_phone()
        otp.verify_code(db, user.phone, "delete", body.code, user_id=user.id)
    try:
        result = request_deletion(db, user, cancel_open=body.cancel_open)
    except DeletionBlocked as exc:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "open": exc.open}},
                            status_code=409)
    if result["status"] == "deleted":
        _end_cookie(response)
    return result


@me_router.get("/export")
def export_my_data(user: CurrentUser, db: DB):
    """Everything NearShop holds about the account, as a JSON download (3 a day, app/core/ratelimit.py)."""
    data = export_data(db, user)
    return JSONResponse(data, headers={
        "Content-Disposition": f'attachment; filename="nearshop-export-{user.id}.json"',
        "Cache-Control": "no-store"})
