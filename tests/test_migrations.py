"""Alembic migrations (D-10.11): fresh databases and Step-1 databases reach the model schema."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Table, create_engine, inspect, text

from conftest import PG_URL, reset_postgres
from core.db import Base, init_engine
from core.migrate import upgrade_database
from core.models import User


def _diff(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)


def test_fresh_database_matches_models(tmp_path):
    url = f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    init_engine(url)
    upgrade_database()
    assert _diff(url) == []
    upgrade_database()  # idempotent


@pytest.mark.skipif(not PG_URL, reason="needs TEST_DATABASE_URL (Postgres)")
def test_fresh_postgres_matches_models():
    reset_postgres(PG_URL)
    init_engine(PG_URL)
    upgrade_database()
    assert _diff(PG_URL) == []
    upgrade_database()  # idempotent


def test_step1_database_is_stamped_and_upgraded(tmp_path):
    url = f"sqlite:///{(tmp_path / 'step1.db').as_posix()}"
    engine = init_engine(url)
    users: Table = User.__table__
    users.create(engine)  # what Step 1's create_all produced
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (username, display_name, password_hash, role, is_active, must_change_password,"
            " session_version, failed_logins, created_at, updated_at) VALUES"
            " ('keep.me', 'x', 'h', 'admin', 1, 0, 1, 0, '2026-10-07', '2026-10-07')"
        ))
    upgrade_database()
    assert "analysis_requests" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT username FROM users")).scalar() == "keep.me"
    assert _diff(url) == []


def test_sqlite_table_rebuild_keeps_referencing_rows(tmp_path):
    """0004 rebuilds api_keys/llm_models on SQLite; cascading FKs must not delete their usage rows."""
    from alembic import command

    from core.migrate import alembic_config

    url = f"sqlite:///{(tmp_path / 'rebuild.db').as_posix()}"
    engine = init_engine(url)
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0003_login_tickets")
        conn.execute(text("INSERT INTO providers (id, owner_id, name, api_type, base_url, is_active, created_at)"
                          " VALUES (1, NULL, 'p', 'mock', '', 1, '2026-10-09')"))
        conn.execute(text("INSERT INTO llm_models (id, provider_id, name, display_name, supports_pdf,"
                          " supports_temperature, tpm, max_attempts, timeout_s, is_active)"
                          " VALUES (1, 1, 'm', 'm', 1, 1, 5000, 3, 300, 1)"))
        conn.execute(text("INSERT INTO api_keys (id, provider_id, label, key_encrypted, key_last4, tpm, concurrency,"
                          " is_active, created_at) VALUES (1, 1, 'k', 'x', '1234', 9000, 5, 1, '2026-10-09')"))
        conn.execute(text("INSERT INTO key_usage_daily (api_key_id, model_id, day, requests, input_tokens,"
                          " output_tokens) VALUES (1, 1, '2026-10-09', 7, 1, 1)"))
    upgrade_database()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT requests FROM key_usage_daily")).scalar() == 7
        assert conn.execute(text("SELECT concurrency FROM api_keys")).scalar() == 5
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1  # switched back on
    assert "tpm" not in {c["name"] for c in inspect(engine).get_columns("api_keys")}
    assert _diff(url) == []
