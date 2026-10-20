"""Application settings, loaded from environment variables / .env."""
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
    cookie_secure: bool = False

    # Launch lockdown. Sign-ups default to closed when environment is production and open elsewhere;
    # NEARSHOP_SIGNUPS_OPEN overrides that either way.
    signups_open: bool | None = None
    # The site runs on generated data: every site page shows a banner asking visitors not to enter
    # real personal details.
    demo_mode: bool = False

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
    max_upload_mb: int = 5

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
