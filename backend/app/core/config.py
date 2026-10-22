"""Application settings, loaded from environment variables / .env."""
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]

Environment = Literal["development", "test", "staging", "production", "demo"]
ENVIRONMENTS: tuple[str, ...] = get_args(Environment)
MediaBackend = Literal["local", "s3"]
BackupBackend = Literal["off", "local", "s3"]
RateLimitMode = Literal["enforce", "log_only", "off"]
EDGE_SECRET_MIN_LENGTH = 32
# The deployed environments, served over https: the API refuses to start in them with cookie_secure false.
SECURE_COOKIE_ENVIRONMENTS: tuple[str, ...] = ("staging", "production", "demo")
# The email sign-in sunset (P4.2, NEARSHOP_EMAIL_LOGIN_SUNSET): at least 8 weeks after phone sign-in starts.
DEFAULT_EMAIL_LOGIN_SUNSET = date(2027, 4, 10)
# Development's default instead: there the console provider makes phone sign-in available while the seeded accounts
# sign in with email and have no verified phone, so the real date would lock a developer's (and the CI lab's) demo
# accounts out of email sign-in once it passed. Set NEARSHOP_EMAIL_LOGIN_SUNSET to rehearse the sunset locally.
DEVELOPMENT_EMAIL_LOGIN_SUNSET = date(2100, 1, 1)
# The least notice between the first day phone sign-in is available and the sunset (P4.2: at least 8 weeks).
EMAIL_LOGIN_MIN_NOTICE_DAYS = 56


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_prefix="NEARSHOP_", extra="ignore")

    app_name: str = "NearShop"
    # One of ENVIRONMENTS; anything else stops startup (see _check_environment).
    environment: Environment = "development"

    # Database: MongoDB (Atlas or any replica set; transactions need a replica set). Development uses the
    # local single-node replica set from docker-compose.yml (make db-up).
    mongodb_uri: str = "mongodb://localhost:27017/?replicaSet=rs0"
    mongodb_db: str = "nearshop"

    # Environment guard for destructive commands (database/schema.py, check_reset_allowed): reset and the
    # seed refuse the production database name and any URI whose host is, or ends in, one of the
    # comma-separated protected hosts (the staging and production cluster hosts). List an Atlas cluster
    # by its project domain (abcd1.mongodb.net); a cluster name (cluster0.abcd1.mongodb.net) covers it too.
    production_mongodb_db: str = "nearshop_prod"
    protected_hosts: str = ""

    # Auth. secret_key MUST be overridden outside development.
    secret_key: str = "dev-only-change-me"
    access_token_minutes: int = 60 * 24 * 7
    cookie_name: str = "nearshop_session"
    # Secure on the session cookie: browsers then send it over https only. False suits http://localhost in
    # development and test; the API refuses to start with it false in staging, production and demo
    # (insecure_cookie_problem, checked in app/main.py). Not checked here: migrations, backups and cron jobs
    # load these settings too and never set a cookie.
    cookie_secure: bool = False

    # Launch lockdown. Sign-ups default to closed when environment is production and open elsewhere;
    # NEARSHOP_SIGNUPS_OPEN overrides that either way.
    signups_open: bool | None = None
    # The site runs on generated data: every site page shows a banner asking visitors not to enter
    # real personal details.
    demo_mode: bool = False
    # The privacy notice version that registrations consent to (P3.1): the version id of the newest
    # docs/legal/privacy-<version>.md, which the /privacy page shows. Change it when a new version is published.
    notice_version: str = "v1"
    # The grievance officer (P3.8, plan check 6.4), shown on the public /grievance page. B10 appoints the officer;
    # until all five are set, GET /api/grievances/officer answers clearly marked placeholder details instead
    # (placeholder: true). Set all five on production (plan step 3-A).
    grievance_officer_name: str = ""
    grievance_officer_designation: str = ""
    grievance_officer_email: str = ""
    grievance_officer_phone: str = ""
    grievance_officer_address: str = ""

    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Photos (app/services/media.py). local: files in media_dir, served by the API at /media (development
    # and test). s3: an S3-compatible bucket (Cloudflare R2) for staging and production, whose public custom
    # domain is media_public_base_url; the endpoint, bucket and key pair are required then. Image URLs use
    # media_public_base_url whenever it is set, and /media otherwise.
    media_backend: MediaBackend = "local"
    media_dir: Path = BACKEND_DIR / "media"
    media_s3_endpoint: str = ""  # https://<account id>.r2.cloudflarestorage.com
    media_s3_bucket: str = ""
    media_s3_access_key_id: str = ""
    media_s3_secret_access_key: SecretStr = SecretStr("")
    media_s3_region: str = "auto"  # R2 uses auto
    media_public_base_url: str = ""  # https://media.example.com, no trailing slash needed
    # Private KYC documents (P3.3, app/services/media.py): shop document and storefront photos, never public.
    # local backend: files under kyc_media_dir, which nothing serves (outside media_dir); admins open them only
    # through 5-minute signed API links. s3 backend: kyc_s3_bucket, a separate bucket with no public access,
    # reached with the media endpoint and key pair; admins get 5-minute presigned links. Until it is set (and
    # different from media_s3_bucket), KYC submission answers 503 kyc_storage_unavailable.
    kyc_media_dir: Path = BACKEND_DIR / "private_media"
    kyc_s3_bucket: str = ""

    # Upload and import limits (P2.7, plan section 8.13). Every request body over max_request_body_mb is
    # refused with 413 before it is parsed, on every route (app/main.py). Photos: at most max_upload_mb,
    # and refused from their header, before decoding, above max_image_pixels or max_image_side px on a
    # side (app/services/media.py). Stock imports (app/services/importer.py): at most import_max_mb; a
    # .xlsx whose archive expands beyond import_max_uncompressed_mb, or holds an entry compressed more
    # than import_max_compression_ratio to 1, is refused before parsing; reading stops at
    # import_max_rows data rows plus one and at the first row wider than import_max_columns.
    max_request_body_mb: int = 6
    max_upload_mb: int = 5
    max_image_pixels: int = 25_000_000
    max_image_side: int = 8000
    import_max_mb: int = 5
    import_max_uncompressed_mb: int = 50
    import_max_compression_ratio: int = 100
    import_max_rows: int = 2000
    import_max_columns: int = 100

    @field_validator("max_request_body_mb", "max_upload_mb", "max_image_pixels", "max_image_side", "import_max_mb",
                     "import_max_uncompressed_mb", "import_max_compression_ratio", "import_max_rows",
                     "import_max_columns", mode="after")
    @classmethod
    def _check_upload_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("NEARSHOP_MAX_* and NEARSHOP_IMPORT_MAX_* limits must be 1 or more")
        return value

    # Off-site backups (database/backup.py and database/restore.py; docs/runbooks/restore.md). off (the
    # default): the tools refuse to run. local: sets in the directory backup_dir (development and drills
    # on a laptop's local database). s3: the private Cloudflare R2 backup bucket, never a photo bucket; the
    # endpoint, bucket and key pair are required then. Sets go under <backup_prefix>/<set id>/, and the
    # prefix defaults to the environment name, so staging and production never mix even in one bucket.
    # The API never reads these; only the two tools do, and they check them when they start.
    backup_backend: BackupBackend = "off"
    backup_dir: Path | None = None
    backup_prefix: str = ""
    backup_s3_endpoint: str = ""  # https://<account id>.r2.cloudflarestorage.com
    backup_s3_bucket: str = ""
    backup_s3_access_key_id: str = ""
    backup_s3_secret_access_key: SecretStr = SecretStr("")
    backup_s3_region: str = "auto"
    # Retention (plan section 8.4): the newest set of each of the last 30 days, and the first set of
    # each of the last 12 months, counting the current day and month. Every other set is deleted.
    backup_keep_daily: int = 30
    backup_keep_monthly: int = 12

    # Seed / location defaults
    seed_city: str = "ballari"
    # Password given to the seeded demo accounts. No default: the seed refuses to run until a long
    # random value is set in the environment or backend/.env (see database/seed/seed.py, check_demo_password).
    demo_password: SecretStr = SecretStr("")

    # Fulfillment defaults
    reservation_hold_minutes: int = 30
    request_response_minutes: int = 20  # shop must respond to a reservation request within this window
    # Reviews (P3.5, plan section 8.2): only from a verified phone (User.phone_verified_at). On since P4.1
    # wherever a customer can verify a phone: where phone sign-in is live (otp_live), and in development, where
    # the console provider verifies phones. A decision that departs from the prompt's "turn it on": it stays off
    # in staging, production and demo until a provider is configured there (B7, B8), since no customer could
    # verify a phone before then and every review would be refused; and off in test, whose review tests turn it
    # on where they test it. NEARSHOP_REVIEW_REQUIRES_VERIFIED_PHONE=true or false overrides that either way.
    review_requires_verified_phone: bool | None = None

    # Phone one-time codes (P4.1, app/services/otp.py and app/services/messaging.py; plan sections 8.3 and 8.10).
    # Phone sign-in is available only while otp_enabled holds and a provider is configured: WhatsApp (Meta's
    # authentication template, the primary channel) and SMS through a DLT-registered sender and template (the
    # fallback). Until then code requests answer 503 otp_unavailable and GET /api/auth/phone-signin reports it as
    # unavailable. The console provider (codes written to the log) stands in for both, and only in development
    # and test, where it is on by default; it can never be turned on anywhere else.
    otp_enabled: bool = True
    otp_console: bool | None = None
    # The global daily cap on code sends (IST days). At 80% of it a warning event goes to the error tracker, once a
    # day (the OTP-spend alert); beyond it code requests answer 503 otp_unavailable.
    otp_daily_cap: int = 2000
    # The site's domain on the SMS template's last line ("@<domain> #<code>"), which Android Chrome's WebOTP reads
    # to fill the code in. It must match the text registered under DLT (B7) for this environment's site.
    otp_webotp_domain: str = ""
    # WhatsApp Cloud API (B8: Meta business verification and the approved authentication template, whose
    # copy-code button carries the code). All three of the token, phone number id and template name are needed.
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_otp_template: str = ""
    whatsapp_template_language: str = "en"
    whatsapp_api_base: str = "https://graph.facebook.com"
    whatsapp_api_version: str = "v21.0"
    whatsapp_cost_inr: float = 0.13  # per authentication message (plan section 8.3: about ₹260 for 2,000)
    # Outbound WhatsApp notifications (P4.3, app/services/whatsapp.py; docs/whatsapp-templates.md). Off by default:
    # while off, notify() writes no outbox rows and the sender job sends nothing. On, rows are written for recipients
    # who consented to WhatsApp messages and have a verified phone, and sent through the Cloud API with the access
    # token and phone number id above and the approved utility templates (B8). In development and test a fake client
    # stands in (messages kept in memory and logged); the test suite never reaches the Cloud API.
    whatsapp_notifications: bool = False
    # The Meta app's secret, which signs every webhook call (X-Hub-Signature-256), and the verify token typed into the
    # webhook's settings in Meta's app dashboard (the GET challenge). Without them the webhook refuses every call.
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_webhook_verify_token: SecretStr = SecretStr("")
    whatsapp_utility_cost_inr: float = 0.16  # per utility template outside a service window (plan section 8.7)
    # SMS through MSG91's flow API (B7: the DLT entity, sender header and OTP template; the flow template at MSG91
    # holds that registered text and its code variable). The auth key and template id are needed.
    sms_auth_key: SecretStr = SecretStr("")
    sms_template_id: str = ""
    sms_code_variable: str = "otp"
    sms_api_url: str = "https://control.msg91.com/api/v5/flow"
    sms_cost_inr: float = 0.295  # ₹0.25 plus 18% GST per message (plan section 8.3: about ₹590 for 2,000)

    # The email sign-in sunset (P4.2): from this IST day, customers and owners without a verified phone can no longer
    # sign in with email and password, and the site sends a signed-in one to /verify-phone on every page. Before it,
    # from the day phone sign-in is available, they see a banner with this date. It must be at least 8 weeks after
    # phone sign-in starts in production (10 Feb 2027 at the latest for 10 Apr 2027). The sunset takes effect only
    # where phone sign-in is available: without a provider, nobody could verify a phone, so email sign-in stays.
    # The daily phone_migration job (app/jobs.py) warns these users 30, 7 and 1 days before it and switches off any
    # still unverified 30 days after it. Unset, it is DEFAULT_EMAIL_LOGIN_SUNSET, except in development
    # (DEVELOPMENT_EMAIL_LOGIN_SUNSET). Staging sets it to yesterday, with the minimum notice below at 0, to rehearse
    # the sunset.
    email_login_sunset: date | None = None
    # The guard on that date (P4.2): the sunset in force is never earlier than this many days after the first IST day
    # the phone_migration job saw phone sign-in available here (kept on its lease, app/jobs.py), so a provider that
    # goes live late moves the sunset, the warnings and the switch-off later instead of switching accounts off
    # without warning. Production refuses anything under EMAIL_LOGIN_MIN_NOTICE_DAYS; staging sets 0 to rehearse.
    email_login_min_notice_days: int = EMAIL_LOGIN_MIN_NOTICE_DAYS

    @field_validator("email_login_sunset", mode="before")
    @classmethod
    def _blank_sunset(cls, value: object) -> object:
        # An empty value (a Render variable left blank) means the environment's default date.
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _default_email_login_sunset(self) -> "Settings":
        if self.email_login_sunset is None:
            self.email_login_sunset = (DEVELOPMENT_EMAIL_LOGIN_SUNSET if self.environment == "development"
                                       else DEFAULT_EMAIL_LOGIN_SUNSET)
        if self.email_login_min_notice_days < 0:
            raise ValueError("NEARSHOP_EMAIL_LOGIN_MIN_NOTICE_DAYS must be 0 or more")
        if self.environment == "production" and self.email_login_min_notice_days < EMAIL_LOGIN_MIN_NOTICE_DAYS:
            raise ValueError(f"NEARSHOP_EMAIL_LOGIN_MIN_NOTICE_DAYS must be {EMAIL_LOGIN_MIN_NOTICE_DAYS} or more in "
                             "production: customers and owners get at least 8 weeks of warning")
        return self

    @field_validator("otp_daily_cap", mode="after")
    @classmethod
    def _check_otp_cap(cls, value: int) -> int:
        if value < 1:
            raise ValueError("NEARSHOP_OTP_DAILY_CAP must be 1 or more")
        return value

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_access_token.get_secret_value().strip() and self.whatsapp_phone_number_id.strip()
                    and self.whatsapp_otp_template.strip())

    @property
    def whatsapp_notify_configured(self) -> bool:
        """The Cloud API can send notification templates: the access token and phone number id are set."""
        return bool(self.whatsapp_access_token.get_secret_value().strip() and self.whatsapp_phone_number_id.strip())

    @property
    def sms_configured(self) -> bool:
        return bool(self.sms_auth_key.get_secret_value().strip() and self.sms_template_id.strip())

    @property
    def otp_live(self) -> bool:
        """Phone sign-in through a real provider: on, with WhatsApp or SMS configured, and the rate limiter
        counting (not NEARSHOP_RATE_LIMIT_MODE=off): its phone-code rules refuse even in log_only
        (app/core/ratelimit.py), and without them a code is a few guesses away and the daily cap one caller's."""
        return (self.otp_enabled and self.rate_limit_mode not in ("off", None)
                and (self.whatsapp_configured or self.sms_configured))

    @model_validator(mode="after")
    def _default_otp(self) -> "Settings":
        if self.otp_console is None:
            self.otp_console = self.environment in ("development", "test")
        elif self.otp_console and self.environment not in ("development", "test"):
            raise ValueError("NEARSHOP_OTP_CONSOLE is allowed only in development and test: it writes codes to the log")
        return self
    # Completion codes (P3.5): completing a pickup needs the reservation code and delivering an order needs the
    # customer's 4-digit delivery code. On since P3.7, whose owner screens ask for the code; the frontend sends it
    # either way, so NEARSHOP_COMPLETION_CODES_REQUIRED=false is a safe switch back. Delivery codes are created
    # either way.
    completion_codes_required: bool = True

    # Plans and subscriptions (P9.4, app/services/plans.py; plan section 8.18). The prices and entitlements are data
    # (the plans collection, versioned); these are the rules around them, all assumptions under test
    # (business/BUSINESS_MODEL.md), so they are settings, never constants in code.
    # The IST day paid plans start: advance subscriptions become active for one year on it, and no shop-paid plan is
    # recorded before it (until then a shop can only pay a refundable advance).
    billing_starts_on: date = date(2027, 8, 1)
    # The founding offer: Growth at the catalogue's founding price for the first year, for the first founding_offer_cap
    # shops in these cities (comma-separated, compared without case), counted in order of first payment, advances
    # included. The cap is enforced in a transaction (plans.claim_founding_slot).
    founding_offer_cap: int = 100
    founding_offer_cities: str = "Ballari,Bellary"
    # The outcome guarantee: a paid quarter that ends with fewer than this many customer requests (P9.2) adds
    # plan_guarantee_credit_months to the subscription's end date.
    plan_guarantee_min_requests: int = 3
    plan_guarantee_credit_months: int = 3
    # An advance must be used within 365 days of its payment (plan section 8.18); the admins are alerted when one is
    # still not active this many days after it.
    advance_alert_days: int = 300

    @field_validator("founding_offer_cap", "plan_guarantee_min_requests", "plan_guarantee_credit_months",
                     "advance_alert_days", mode="after")
    @classmethod
    def _check_plan_numbers(cls, value: int) -> int:
        if value < 0:
            raise ValueError("NEARSHOP_FOUNDING_OFFER_CAP, NEARSHOP_PLAN_GUARANTEE_* and NEARSHOP_ADVANCE_ALERT_DAYS "
                             "must be 0 or more")
        return value

    @property
    def founding_city_list(self) -> list[str]:
        """NEARSHOP_FOUNDING_OFFER_CITIES as lower-case names, without blanks."""
        return [c.strip().lower() for c in self.founding_offer_cities.split(",") if c.strip()]

    # AI
    embedding_provider: str = "fastembed"  # fastembed | tfidf
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    model_dir: Path = BACKEND_DIR / "ml_artifacts"
    enable_scheduler: bool = True
    warm_on_startup: bool = True  # build the semantic index when the API starts (the schema version check always runs)

    # Observability (app/core/observability.py). Error tracking is on only when sentry_dsn is set, from the
    # environment (Render: NEARSHOP_SENTRY_DSN, a sync: false secret), never from code. release is the git
    # commit events carry; blank means Render's RENDER_GIT_COMMIT. Trace sample rates are 0 to 1.
    sentry_dsn: SecretStr = SecretStr("")
    sentry_traces_sample_rate: float = 0.1
    sentry_search_traces_sample_rate: float = 0.25
    release: str = ""
    # The request id comes from the first of these incoming headers that holds a well-formed id (the
    # edge's own id first), else a new random one. Comma-separated, case-insensitive.
    request_id_headers: str = "x-vercel-id,x-request-id"

    # Edge-only access (P2.3, app/core/ratelimit.py). Vercel's proxy (frontend/src/proxy.ts) sends edge_secret in
    # X-NearShop-Edge on every /api and /media request, with the visitor's address in X-NearShop-Client-IP. The
    # same value is set on Render (NEARSHOP_EDGE_SECRET, sync: false) and Vercel (NEARSHOP_EDGE_SECRET, server
    # only, never NEXT_PUBLIC_): 32 or more random characters. edge_secret_required defaults to true in staging
    # and production, where the API then refuses to start without the secret and answers 403 to any request
    # without it (/api/health and /api/health/ready excepted); NEARSHOP_EDGE_SECRET_REQUIRED overrides it.
    edge_secret: SecretStr = SecretStr("")
    edge_secret_required: bool | None = None
    # Rate limits (plan section 8.3). enforce: over a limit the API answers 429; log_only: it logs what it
    # would have limited and serves the request; off: nothing is counted. The default is log_only in staging
    # and production (48 hours of it before NEARSHOP_RATE_LIMIT_MODE=enforce is set), off in test (the rate
    # limit tests switch it on) and enforce in development and demo.
    rate_limit_mode: RateLimitMode | None = None

    # Storage budget (P2.2, app/services/analytics.py): the Atlas tier's storage limit in MB (MiB), which counts
    # uncompressed documents plus indexes. M0 (staging, demo) 512; Flex (production after plan step 1-A) 5120.
    # The admin storage card and the daily snapshot's 70% and 85% warnings are measured against it.
    storage_limit_mb: int = 512

    @field_validator("storage_limit_mb", mode="after")
    @classmethod
    def _check_storage_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("NEARSHOP_STORAGE_LIMIT_MB must be 1 or more")
        return value

    @field_validator("sentry_traces_sample_rate", "sentry_search_traces_sample_rate", mode="after")
    @classmethod
    def _check_sample_rate(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("NEARSHOP_SENTRY_TRACES_SAMPLE_RATE and NEARSHOP_SENTRY_SEARCH_TRACES_SAMPLE_RATE "
                             "must be between 0 and 1")
        return value

    @field_validator("environment", mode="before")
    @classmethod
    def _check_environment(cls, value: object) -> object:
        normalised = value.strip().lower() if isinstance(value, str) else value
        if normalised not in ENVIRONMENTS:
            raise ValueError(f"NEARSHOP_ENVIRONMENT must be one of {', '.join(ENVIRONMENTS)}; got {value!r}")
        return normalised

    @field_validator("media_backend", mode="before")
    @classmethod
    def _check_media_backend(cls, value: object) -> object:
        # An empty value (a Render variable left blank) means the default.
        normalised = (value.strip().lower() or "local") if isinstance(value, str) else value
        if normalised not in get_args(MediaBackend):
            raise ValueError(f"NEARSHOP_MEDIA_BACKEND must be local or s3; got {value!r}")
        return normalised

    @field_validator("backup_backend", mode="before")
    @classmethod
    def _check_backup_backend(cls, value: object) -> object:
        # An empty value (a Render variable left blank) means off.
        normalised = (value.strip().lower() or "off") if isinstance(value, str) else value
        if normalised not in get_args(BackupBackend):
            raise ValueError(f"NEARSHOP_BACKUP_BACKEND must be off, local or s3; got {value!r}")
        return normalised

    @field_validator("backup_dir", mode="before")
    @classmethod
    def _blank_backup_dir(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("backup_keep_daily", "backup_keep_monthly", mode="after")
    @classmethod
    def _check_backup_keep(cls, value: int) -> int:
        if value < 1:
            raise ValueError("NEARSHOP_BACKUP_KEEP_DAILY and NEARSHOP_BACKUP_KEEP_MONTHLY must be 1 or more")
        return value

    @property
    def backup_set_prefix(self) -> str:
        """Where this environment's backup sets live in the store: NEARSHOP_BACKUP_PREFIX, or the
        environment name, without leading or trailing slashes."""
        return self.backup_prefix.strip().strip("/") or self.environment

    @field_validator("media_public_base_url", mode="after")
    @classmethod
    def _check_media_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("NEARSHOP_MEDIA_PUBLIC_BASE_URL must start with https:// "
                             "(or http:// in development and test)")
        return value

    @model_validator(mode="after")
    def _check_media_s3(self) -> "Settings":
        # The deployed site is https, and browsers block or flag http images on an https page.
        if self.media_public_base_url.startswith("http://") and self.environment not in ("development", "test"):
            raise ValueError(f"NEARSHOP_MEDIA_PUBLIC_BASE_URL must start with https:// in {self.environment}; "
                             "http:// is accepted only in development and test")
        if self.media_backend == "s3":
            required = {
                "NEARSHOP_MEDIA_S3_ENDPOINT": self.media_s3_endpoint,
                "NEARSHOP_MEDIA_S3_BUCKET": self.media_s3_bucket,
                "NEARSHOP_MEDIA_S3_ACCESS_KEY_ID": self.media_s3_access_key_id,
                "NEARSHOP_MEDIA_S3_SECRET_ACCESS_KEY": self.media_s3_secret_access_key.get_secret_value(),
                # Nothing else serves the bucket's photos: the API mounts /media only for the local backend.
                "NEARSHOP_MEDIA_PUBLIC_BASE_URL": self.media_public_base_url,
            }
            if missing := [name for name, value in required.items() if not value.strip()]:
                raise ValueError(f"NEARSHOP_MEDIA_BACKEND=s3 needs {', '.join(missing)}")
        return self

    def insecure_cookie_problem(self) -> str | None:
        """Why the API must not start with these cookie settings, or None (P2.6)."""
        if self.environment in SECURE_COOKIE_ENVIRONMENTS and not self.cookie_secure:
            return (f"Set NEARSHOP_COOKIE_SECURE=true before running in {self.environment}: the session cookie "
                    "must never be sent over plain http")
        return None

    @property
    def protected_host_list(self) -> list[str]:
        """NEARSHOP_PROTECTED_HOSTS as lower-case host names, without ports or blanks."""
        hosts = (h.strip().lower() for h in self.protected_hosts.split(","))
        return [h.rsplit(":", 1)[0] if h.count(":") == 1 else h for h in hosts if h]

    @model_validator(mode="after")
    def _default_signups_open(self) -> "Settings":
        if self.signups_open is None:
            self.signups_open = self.environment != "production"
        return self

    @field_validator("rate_limit_mode", mode="before")
    @classmethod
    def _check_rate_limit_mode(cls, value: object) -> object:
        # An empty value (a Render variable left blank) means the environment's default.
        normalised = (value.strip().lower().replace("-", "_") or None) if isinstance(value, str) else value
        if normalised is not None and normalised not in get_args(RateLimitMode):
            raise ValueError(f"NEARSHOP_RATE_LIMIT_MODE must be enforce, log_only or off; got {value!r}")
        return normalised

    @field_validator("edge_secret", mode="after")
    @classmethod
    def _check_edge_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value().strip()
        if secret and len(secret) < EDGE_SECRET_MIN_LENGTH:
            raise ValueError(f"NEARSHOP_EDGE_SECRET must be {EDGE_SECRET_MIN_LENGTH} or more characters")
        return SecretStr(secret)

    @model_validator(mode="after")
    def _default_edge_and_rate_limits(self) -> "Settings":
        # Not an error here when the secret is missing: migrations, backups and cron jobs load these settings
        # too and never need it. The API checks it when it starts (app/main.py).
        if self.edge_secret_required is None:
            self.edge_secret_required = self.environment in ("staging", "production")
        if self.rate_limit_mode is None:
            self.rate_limit_mode = {"staging": "log_only", "production": "log_only", "test": "off"}.get(
                self.environment, "enforce")
        return self

    @model_validator(mode="after")
    def _default_review_rule(self) -> "Settings":
        # After the rate-limit default, which otp_live reads.
        if self.review_requires_verified_phone is None:
            self.review_requires_verified_phone = self.otp_live or (
                self.environment == "development" and self.otp_enabled and bool(self.otp_console))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
