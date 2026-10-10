import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fda-tests-"))

# Isolated test configuration (never touches .env, a real database or real providers).
os.environ.update(
    {
        "APP_ENV": "development",
        "APP_SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
        "APP_ENCRYPTION_KEY": "q7bLbKk4m4xQ0b2pQ7o3w5a0Zr7sQJb8m5o9cM2YH2Y=",
        "DATABASE_URL": "sqlite://",
        "SUPABASE_URL": "",
        "LOCAL_STORAGE_DIR": str(_TMP / "storage"),
        "ADMIN_USERNAME": "admin",
        "ADMIN_INITIAL_PASSWORD": "Admin1234",
        "GUEST_RESULT_TTL_MINUTES": "120",
    }
)


# Optional: TEST_DATABASE_URL=postgresql+psycopg://... runs the database tests against Postgres
# (a throwaway database: its public schema is dropped before every test). Default: SQLite files.
PG_URL = os.getenv("TEST_DATABASE_URL", "").strip()


def reset_postgres(url: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()


@pytest.fixture()
def db(tmp_path):
    from core.config import get_settings
    from core.db import create_schema, init_engine
    from services.app_settings import clear_cache
    from services.storage import get_storage

    get_settings.cache_clear()
    get_storage.cache_clear()
    clear_cache()
    if PG_URL:
        reset_postgres(PG_URL)
        engine = init_engine(PG_URL)
    else:
        # A file database, like production: every thread gets its own connection. (An in-memory
        # database shares one connection between the engine's worker threads, which races.)
        engine = init_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    create_schema()
    yield
    engine.dispose()
