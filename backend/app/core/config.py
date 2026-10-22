"""Application settings, loaded from environment variables / .env."""
from dataclasses import dataclass
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


@dataclass(frozen=True)
class EmbeddingModelSpec:
    """How one embedding model is run and calibrated (P5.3; trial results in docs/perf/embedding-models.md).

    query_prefix and passage_prefix are prepended to search queries and to listing texts, for models trained
    with them (multilingual E5: "query: " and "passage: "); empty for models that need none. sem_floor and
    sem_ceil are the cosine range that search maps to 0..1 for this model. They are not tuned here (P5.5):
    every candidate starts on the calibration of the current model.
    """

    query_prefix: str = ""
    passage_prefix: str = ""
    sem_floor: float = 0.22
    sem_ceil: float = 0.62


# The cosine scale search ranks on: the calibration of all-MiniLM-L6-v2 (the sem_floor and sem_ceil of the r1
# ranking config below). The semantic index maps another model's similarities onto this scale with that model's
# own floor and ceiling below, so search.py stays unchanged when the model changes.
SEARCH_SIMILARITY_SCALE = EmbeddingModelSpec()

# The embedding models NEARSHOP_EMBEDDING_MODEL may name: those the installed fastembed (0.8.1) supports
# that were considered in P5.3. A model is listed here before it can be selected, so its prefixes are
# never missed.
EMBEDDING_MODELS: dict[str, EmbeddingModelSpec] = {
    # The current model (English only, 384 dimensions, 90 MB).
    "sentence-transformers/all-MiniLM-L6-v2": EmbeddingModelSpec(),
    # P5.3 candidates (multilingual); calibration not tuned.
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": EmbeddingModelSpec(),
    "minishlab/potion-multilingual-128M": EmbeddingModelSpec(),
    # Considered but not trialled (too large for the 2 GB instance; see docs/perf/embedding-models.md).
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": EmbeddingModelSpec(),
    "intfloat/multilingual-e5-large": EmbeddingModelSpec(query_prefix="query: ", passage_prefix="passage: "),
}
# The TF-IDF fallback (app/ai/embeddings.py) has no model name of its own in the setting.
TFIDF_MODEL_NAME = "tfidf-char-svd"


def embedding_spec(model_name: str) -> EmbeddingModelSpec:
    """The spec of a listed model; the TF-IDF fallback and anything unlisted get no prefixes and the default
    calibration."""
    return EMBEDDING_MODELS.get(model_name, EmbeddingModelSpec())



@dataclass(frozen=True)
class RankingConfig:
    """Every constant search ranks with (P5.5; app/services/search.py, run_search). Versioned: a config is never
    edited once it has served traffic. A tuned config is added under a new version, with its tuning report in
    docs/search/tuning-report.md, and switched on with NEARSHOP_SEARCH_RANKING_VERSION.

    Relevance of a listing: w_semantic times its similarity mapped from sem_floor..sem_ceil onto 0..1, plus
    w_keyword times its keyword score over the best keyword score (local-term matches add w_local_term each,
    P5.2). A listing with no keyword hit needs a similarity of sem_min_match or more, and listings below
    relative_cutoff times the best relevance are dropped. A group of listings of one item scores
    w_relevance x relevance + w_proximity x proximity + w_in_stock when some offer is in stock, times
    out_of_stock_factor when none is.
    """

    version: str
    sem_floor: float = 0.22
    sem_ceil: float = 0.62
    sem_min_match: float = 0.34
    w_semantic: float = 0.6
    w_keyword: float = 0.4
    relative_cutoff: float = 0.42
    w_local_term: float = 6.0
    w_relevance: float = 0.75
    w_proximity: float = 0.15
    w_in_stock: float = 0.10
    out_of_stock_factor: float = 0.6

    def tunables(self) -> dict[str, float]:
        """The numeric fields, without the version."""
        return {k: v for k, v in self.__dict__.items() if k != "version"}


# r1: the hand-set values search used before P5.5, never measured. Add a tuned config as r2, r3 and so on,
# only after it passes the adoption rule on the test split (plan section 8.6; docs/search/tuning-report.md).
RANKING_CONFIGS: dict[str, RankingConfig] = {
    "r1": RankingConfig(version="r1"),
}
DEFAULT_RANKING_VERSION = "r1"

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
    # Sales history archiving (P8.3, database/archive.py; docs/runbooks/restore.md). A monthly cron job moves the
    # sales_history months that end on or before the first of the current month less archive_keep_months months
    # (18 or more: the models use at most the last 18) to the backup store above, under
    # <backup_prefix>/archive/sales_history/, and deletes their rows once each upload is read back and verified.
    # It refuses to run while backup_backend is off, and unless a backup of the same database succeeded in the
    # last archive_backup_max_age_hours hours.
    archive_keep_months: int = 18
    archive_backup_max_age_hours: int = 26

    @field_validator("archive_keep_months", mode="after")
    @classmethod
    def _check_archive_keep(cls, value: int) -> int:
        if value < 18:
            raise ValueError("NEARSHOP_ARCHIVE_KEEP_MONTHS must be 18 or more: the models use the last 18 months")
        return value

    @field_validator("archive_backup_max_age_hours", mode="after")
    @classmethod
    def _check_archive_backup_age(cls, value: int) -> int:
        if value < 1:
            raise ValueError("NEARSHOP_ARCHIVE_BACKUP_MAX_AGE_HOURS must be 1 or more")
        return value

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
    # One of EMBEDDING_MODELS, by name (P5.3). Changing it re-embeds every listing once, into
    # text_embeddings under the new name; the old model's vectors stay until deleted.
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    model_dir: Path = BACKEND_DIR / "ml_artifacts"
    enable_scheduler: bool = True
    warm_on_startup: bool = True  # build the semantic index when the API starts (the schema version check always runs)

    # Search ranking (P5.5): the version in RANKING_CONFIGS that run_search ranks with.
    search_ranking_version: str = DEFAULT_RANKING_VERSION
    # Suggestion hygiene (P5.5): a query is shown in search suggestions and popular searches only when at least
    # suggestion_min_users distinct signed-in users (counted by user_id, never by session; 5 or more) searched
    # it in the last suggestion_window_days days and got results, it is at most suggestion_max_chars characters
    # long, and it passes the blocklist (phone numbers, email addresses, links and abusive words, plus the
    # comma-separated suggestion_blocklist words added here; app/services/search.py).
    suggestion_min_users: int = 5
    suggestion_window_days: int = 30
    suggestion_max_chars: int = 60
    suggestion_blocklist: str = ""
    # Shadow ranking (P5.6, app/services/search_events.py; plan section 8.6): a sampled search_shadow_share (0 to
    # 1) of the consented page-1 searches with a query is re-ranked after the response is sent, on the same fetched
    # listings and scores, with the ranking config search_shadow_ranking_version, and both rankings are stored on
    # the search event. Empty (the default) or a share of 0 turns it off. Customers always get the production
    # ranking (search_ranking_version). A config passes the plan section 8.6 shadow guardrails before it serves.
    search_shadow_ranking_version: str = ""
    search_shadow_share: float = 0.0

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

    # Model gating (P7.3, plan section 8.9): owners see a model's output only when the data meets these
    # thresholds, and otherwise the reason and the progress towards them (app/services/analytics.py, model
    # gates). models_real_data_only: training and the thresholds leave out is_demo sales; defaults to true in
    # production and false elsewhere (the demo site and development run on seeded sales, which then count).
    models_real_data_only: bool | None = None
    # Demand forecast: a listing is eligible with demand_min_history_days or more days since its first sale,
    # sales on demand_min_sale_days or more distinct days and demand_min_units or more units. Forecasts are
    # shown only when, on the last demand_backtest_days days, the eligible listings' MAE is at least
    # demand_min_improvement_pct percent below the naive same-as-last-week forecast's.
    demand_min_history_days: int = 56
    demand_min_sale_days: int = 20
    demand_min_units: int = 30
    demand_backtest_days: int = 14
    demand_min_improvement_pct: float = 10.0
    # Bundles: bundles_min_baskets or more multi-item baskets city-wide in the last bundles_window_days days.
    bundles_min_baskets: int = 1000
    bundles_window_days: int = 90
    # Price flags: an item is compared across price_flags_min_peers or more shops, and only once the city has
    # price_flags_min_verified_shops or more verified live shops. Owners see flags only once admins have
    # reviewed price_flags_min_reviewed or more and price_flags_min_precision_pct percent of those were upheld
    # (reviewed, not dismissed); until then they stay in the admin queue.
    price_flags_min_peers: int = 4
    price_flags_min_verified_shops: int = 25
    price_flags_min_reviewed: int = 50
    price_flags_min_precision_pct: float = 70.0
    # Owner search insights: a query is shown only when search_insights_min_users or more distinct signed-in
    # users (5 or more) searched it within the shop's radius in the last search_insights_window_days days. The
    # radius is the shop's delivery radius when it delivers, else search_insights_radius_km.
    search_insights_min_users: int = 5
    search_insights_window_days: int = 30
    search_insights_radius_km: float = 3.0

    @field_validator("demand_min_history_days", "demand_min_sale_days", "demand_min_units", "demand_backtest_days",
                     "bundles_min_baskets", "bundles_window_days", "price_flags_min_peers",
                     "price_flags_min_verified_shops", "price_flags_min_reviewed", "search_insights_window_days",
                     mode="after")
    @classmethod
    def _check_gate_count(cls, value: int) -> int:
        if value < 1:
            raise ValueError("NEARSHOP_DEMAND_MIN_*, NEARSHOP_BUNDLES_*, NEARSHOP_PRICE_FLAGS_MIN_* and "
                             "NEARSHOP_SEARCH_INSIGHTS_WINDOW_DAYS must be 1 or more")
        return value

    @field_validator("demand_min_improvement_pct", "price_flags_min_precision_pct", mode="after")
    @classmethod
    def _check_gate_pct(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("NEARSHOP_DEMAND_MIN_IMPROVEMENT_PCT and NEARSHOP_PRICE_FLAGS_MIN_PRECISION_PCT must be "
                             "between 0 and 100")
        return value

    @field_validator("search_insights_min_users", mode="after")
    @classmethod
    def _check_search_insights_min_users(cls, value: int) -> int:
        if value < 5:  # plan section 8.9: a few people's searches must never reach a shop owner
            raise ValueError("NEARSHOP_SEARCH_INSIGHTS_MIN_USERS must be 5 or more")
        return value

    @field_validator("search_insights_radius_km", mode="after")
    @classmethod
    def _check_search_insights_radius(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("NEARSHOP_SEARCH_INSIGHTS_RADIUS_KM must be more than 0")
        return value

    @field_validator("embedding_model", mode="after")
    @classmethod
    def _check_embedding_model(cls, value: str) -> str:
        if value not in EMBEDDING_MODELS:
            raise ValueError(f"NEARSHOP_EMBEDDING_MODEL must be one of {', '.join(EMBEDDING_MODELS)}; got {value!r}"
                             " (add a new model to EMBEDDING_MODELS in app/core/config.py, with its prefixes)")
        return value

    @field_validator("search_ranking_version", mode="after")
    @classmethod
    def _check_ranking_version(cls, value: str) -> str:
        if value not in RANKING_CONFIGS:
            raise ValueError(f"NEARSHOP_SEARCH_RANKING_VERSION must be one of {', '.join(RANKING_CONFIGS)}; "
                             f"got {value!r}")
        return value

    @field_validator("search_shadow_ranking_version", mode="after")
    @classmethod
    def _check_shadow_ranking_version(cls, value: str) -> str:
        value = value.strip()
        if value and value not in RANKING_CONFIGS:
            raise ValueError(f"NEARSHOP_SEARCH_SHADOW_RANKING_VERSION must be empty (off) or one of "
                             f"{', '.join(RANKING_CONFIGS)}; got {value!r}")
        return value

    @field_validator("search_shadow_share", mode="after")
    @classmethod
    def _check_shadow_share(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("NEARSHOP_SEARCH_SHADOW_SHARE must be between 0 and 1")
        return value

    @property
    def shadow_ranking(self) -> RankingConfig | None:
        """The shadow candidate ranking config, or None when shadow mode is off."""
        if not self.search_shadow_ranking_version or self.search_shadow_share <= 0:
            return None
        return RANKING_CONFIGS.get(self.search_shadow_ranking_version)

    @field_validator("suggestion_min_users", mode="after")
    @classmethod
    def _check_suggestion_min_users(cls, value: int) -> int:
        if value < 5:  # plan section 8.6: a few people must not reach everyone's suggestions
            raise ValueError("NEARSHOP_SUGGESTION_MIN_USERS must be 5 or more")
        return value

    @field_validator("suggestion_window_days", "suggestion_max_chars", mode="after")
    @classmethod
    def _check_suggestion_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("NEARSHOP_SUGGESTION_WINDOW_DAYS and NEARSHOP_SUGGESTION_MAX_CHARS must be 1 or more")
        return value

    @property
    def ranking(self) -> RankingConfig:
        """The ranking config search uses."""
        return RANKING_CONFIGS[self.search_ranking_version]

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
        if self.models_real_data_only is None:
            self.models_real_data_only = self.environment == "production"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
