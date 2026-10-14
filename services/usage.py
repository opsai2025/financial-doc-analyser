"""Resource usage of the whole system vs. the hosting plan's limits (D-16.5).

Database size is measured the way Supabase does (`pg_database_size`); on local SQLite it is the
file size. Kept PDFs are counted once per file (D-3.5), as stored in the bucket.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, text

from core.db import get_engine, session_scope
from core.models import AnalysisRequest, AnalysisResult, Document, RawOutput, UsageEvent, User, UserDocument, utcnow
from services import app_settings

log = logging.getLogger(__name__)

MB = 1024 * 1024
WARN_FRACTION = 0.8  # admins see a warning bar on every page from here (D-16.5)
_BANNER_SECONDS = 600.0


@dataclass(frozen=True)
class TableSize:
    name: str
    bytes: int
    rows: int | None


@dataclass(frozen=True)
class UserUsage:
    user_id: int
    username: str
    display_name: str
    role: str
    requests: int
    docs_24h: int
    results_bytes: int
    pdf_bytes: int


@dataclass(frozen=True)
class SystemUsage:
    db_bytes: int
    db_limit_bytes: int
    storage_bytes: int
    storage_files: int
    storage_limit_bytes: int
    tables: list[TableSize]

    @property
    def db_fraction(self) -> float:
        return self.db_bytes / self.db_limit_bytes if self.db_limit_bytes else 0.0

    @property
    def storage_fraction(self) -> float:
        return self.storage_bytes / self.storage_limit_bytes if self.storage_limit_bytes else 0.0


def _is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"


def database_bytes() -> int:
    with get_engine().connect() as conn:
        if _is_postgres():
            return int(conn.scalar(text("SELECT pg_database_size(current_database())")) or 0)
        page_count = conn.exec_driver_sql("PRAGMA page_count").scalar() or 0
        page_size = conn.exec_driver_sql("PRAGMA page_size").scalar() or 0
        return int(page_count * page_size)


def table_sizes(limit: int = 8) -> list[TableSize]:
    """Largest tables of the app's schema, indexes included (Postgres only; SQLite returns [])."""
    if not _is_postgres():
        return []
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT c.relname, pg_total_relation_size(c.oid), c.reltuples::bigint "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = current_schema() "
            "ORDER BY 2 DESC LIMIT :limit"), {"limit": limit})
        return [TableSize(name, int(size), int(count) if count is not None and count >= 0 else None)
                for name, size, count in rows]


def stored_pdfs() -> tuple[int, int]:
    """(bytes, files) of PDFs physically kept in storage."""
    with session_scope() as db:
        size, count = db.execute(select(func.coalesce(func.sum(Document.size_bytes), 0), func.count())
                                 .where(Document.storage_path.is_not(None))).one()
    return int(size or 0), int(count or 0)


def system_usage() -> SystemUsage:
    limits = app_settings.get_many(("db_limit_mb", "storage_limit_mb"))
    pdf_bytes, pdf_files = stored_pdfs()
    return SystemUsage(
        db_bytes=database_bytes(), db_limit_bytes=limits["db_limit_mb"] * MB,
        storage_bytes=pdf_bytes, storage_files=pdf_files, storage_limit_bytes=limits["storage_limit_mb"] * MB,
        tables=table_sizes(),
    )


def per_user() -> list[UserUsage]:
    """Every user's share, largest first. PDFs kept by several users count for each of them (D-10.10)."""
    with session_scope() as db:
        since = utcnow() - timedelta(days=1)

        def grouped(query) -> dict[int, int]:
            return {user_id: int(value or 0) for user_id, value in db.execute(query)}

        requests = grouped(select(AnalysisRequest.user_id, func.count()).group_by(AnalysisRequest.user_id))
        docs = grouped(select(UsageEvent.user_id, func.count()).where(UsageEvent.at >= since)
                       .group_by(UsageEvent.user_id))
        results = grouped(select(AnalysisResult.user_id, func.sum(AnalysisResult.size_bytes))
                          .group_by(AnalysisResult.user_id))
        for user_id, size in grouped(select(RawOutput.user_id, func.sum(RawOutput.size_bytes))
                                     .group_by(RawOutput.user_id)).items():
            results[user_id] = results.get(user_id, 0) + size
        pdfs = grouped(select(UserDocument.user_id, func.sum(Document.size_bytes))
                       .join(Document, Document.id == UserDocument.document_id)
                       .where(Document.storage_path.is_not(None)).group_by(UserDocument.user_id))
        users = list(db.scalars(select(User)))
    rows = [UserUsage(u.id, u.username, u.display_name, str(u.role), requests.get(u.id, 0), docs.get(u.id, 0),
                      results.get(u.id, 0), pdfs.get(u.id, 0)) for u in users]
    return sorted(rows, key=lambda r: (-(r.results_bytes + r.pdf_bytes), r.username))


_banner: tuple[float, list[tuple[str, float]]] | None = None
_banner_lock = threading.Lock()


def clear_cache() -> None:
    global _banner
    with _banner_lock:
        _banner = None


def warnings_for_admin() -> list[tuple[str, float]]:
    """[(resource, fraction)] at or above WARN_FRACTION. Measured at most every 10 minutes; never raises."""
    global _banner
    now = time.monotonic()
    with _banner_lock:
        if _banner and _banner[0] > now:
            return _banner[1]
    try:
        usage = system_usage()
        found = [(name, fraction) for name, fraction in (("db", usage.db_fraction), ("storage", usage.storage_fraction))
                 if fraction >= WARN_FRACTION]
    except Exception:  # the banner must never break a page
        log.exception("Usage check failed")
        found = []
    with _banner_lock:
        _banner = (now + _BANNER_SECONDS, found)
    return found
