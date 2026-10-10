"""Alembic environment: uses the app's engine (core.db) and models metadata."""

from alembic import context

import core.models  # noqa: F401  (registers all tables)
from core.db import Base, get_engine

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite needs batch mode for ALTER
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    with get_engine().begin() as conn:
        _run(conn)
