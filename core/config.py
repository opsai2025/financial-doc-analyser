"""Application settings loaded from environment variables / `.env` (see `.env.example`)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Defaults; the admin can change these in Settings (services/app_settings.py, D-16.3).
# Pre-filled quotas for newly created users (D-4.7).
DEFAULT_ANALYST_DOCS_PER_HOUR = 10
DEFAULT_ANALYST_DOCS_PER_DAY = 50
DEFAULT_ANALYST_DB_QUOTA_MB = 20
DEFAULT_ANALYST_STORAGE_QUOTA_MB = 100
DEFAULT_GUEST_DOC_CAP = 5
DEFAULT_GUEST_VALID_DAYS = 14

# Uploads (D-10.4)
MAX_PDF_MB = 20
MAX_FILES_PER_BATCH = 50
MAX_ZIP_UNCOMPRESSED_MB = 300
RETRY_BACKOFF_S = (10, 30)  # wait before the 2nd and 3rd attempt (D-10.3)

# Server-wide cap on simultaneous API calls (D-11.11): default, and the range an admin may set.
DEFAULT_MAX_PARALLEL_CALLS = 20
MAX_PARALLEL_CALLS_RANGE = (1, 100)

# Free-plan limits of Supabase (D-3.1), compared with the real usage in Settings (D-16.5)
SUPABASE_DB_LIMIT_MB = 500
SUPABASE_STORAGE_LIMIT_MB = 1024

# Login protection (D-4.7)
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    database_url: str
    supabase_url: str
    supabase_service_key: str
    supabase_storage_bucket: str
    local_storage_dir: Path
    app_secret_key: str
    app_encryption_key: str
    admin_username: str
    admin_initial_password: str
    session_hours: int
    guest_result_ttl_minutes: int

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def uses_local_db(self) -> bool:
        return self.database_url.startswith("sqlite")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        (PROJECT_ROOT / "data").mkdir(exist_ok=True)
        database_url = f"sqlite:///{(PROJECT_ROOT / 'data' / 'local.db').as_posix()}"

    local_storage = Path(os.getenv("LOCAL_STORAGE_DIR", "data/storage").strip() or "data/storage")
    if not local_storage.is_absolute():
        local_storage = PROJECT_ROOT / local_storage

    secret = os.getenv("APP_SECRET_KEY", "").strip()
    if len(secret) < 32:
        raise ConfigError("APP_SECRET_KEY is missing or shorter than 32 characters (see .env.example).")

    return Settings(
        app_env=os.getenv("APP_ENV", "development").strip().lower(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        database_url=database_url,
        supabase_url=os.getenv("SUPABASE_URL", "").strip(),
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY", "").strip(),
        supabase_storage_bucket=os.getenv("SUPABASE_STORAGE_BUCKET", "audit-pdfs").strip(),
        local_storage_dir=local_storage,
        app_secret_key=secret,
        app_encryption_key=os.getenv("APP_ENCRYPTION_KEY", "").strip(),
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip().lower(),
        admin_initial_password=os.getenv("ADMIN_INITIAL_PASSWORD", ""),
        session_hours=_int("SESSION_HOURS", 8),
        guest_result_ttl_minutes=_int("GUEST_RESULT_TTL_MINUTES", 120),
    )
