"""Database engine and session handling (Supabase Postgres or local SQLite, D-3.1/D-3.2)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        in_memory = url in ("sqlite://", "sqlite:///:memory:")
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if in_memory else None,  # one shared connection for in-memory DBs
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    # Supabase session pooler: keep the pool small (free tier connection limits).
    return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=1800)


def init_engine(url: str) -> Engine:
    """Create (or replace) the global engine. Tests call this with an in-memory URL."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = _build_engine(url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        from core.config import get_settings

        init_engine(get_settings().database_url)
    assert _engine is not None
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema() -> None:
    import core.models  # noqa: F401  (register models on Base.metadata)

    Base.metadata.create_all(get_engine())
