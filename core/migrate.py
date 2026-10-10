"""Apply Alembic migrations at startup (D-10.11)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from core.config import PROJECT_ROOT
from core.db import get_engine

BASELINE_REVISION = "0001_users"


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg


def upgrade_database() -> None:
    """Bring the schema to the latest revision.

    Databases created by Step 1 (users table via create_all, no alembic_version) are stamped
    with the baseline revision first, so their data is kept.
    """
    engine = get_engine()
    cfg = alembic_config()
    tables = set(inspect(engine).get_table_names())
    sqlite = engine.dialect.name == "sqlite"
    with engine.connect() as connection:
        if sqlite:
            # SQLite alters a table by copying it and dropping the original; with foreign keys on,
            # that DROP would cascade-delete referencing rows (e.g. key_usage_daily). The pragma
            # only works outside a transaction.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        try:
            with connection.begin():
                cfg.attributes["connection"] = connection
                if "users" in tables and "alembic_version" not in tables:
                    command.stamp(cfg, BASELINE_REVISION)
                command.upgrade(cfg, "head")
            if sqlite and (broken := connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()):
                raise RuntimeError(f"Foreign key check failed after migration: {broken[:5]}")
        finally:
            if sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
