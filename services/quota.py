"""Document quotas and capacity (D-4.4, D-4.5, D-4.8, D-10.10).

* Analysts: documents per rolling hour / rolling 24 h; analyses with own keys are waived.
* Guests: lifetime document cap (also with own keys).
* Only successful new API analyses count; documents already queued are reserved.
* Capacity: results-DB bytes and kept-PDF bytes per user (admins unlimited).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from core.db import session_scope
from core.models import AnalysisResult, Document, RawOutput, Role, UsageEvent, User, UserDocument, utcnow

MB = 1024 * 1024


@dataclass(frozen=True)
class QuotaStatus:
    role: Role
    hour_limit: int | None
    hour_used: int
    day_limit: int | None
    day_used: int
    guest_cap: int | None
    guest_used: int
    db_quota_bytes: int | None
    db_used_bytes: int
    storage_quota_bytes: int | None
    storage_used_bytes: int

    def remaining_docs(self, own_keys: bool, reserved: int = 0) -> int | None:
        """How many more new analyses may start now (None = unlimited)."""
        if self.role is Role.ADMIN:
            return None
        if self.role is Role.VIEWER:
            return 0
        if self.role is Role.GUEST:
            return max(0, (self.guest_cap or 0) - self.guest_used - reserved)
        if own_keys:
            return None
        return max(0, min((self.hour_limit or 0) - self.hour_used, (self.day_limit or 0) - self.day_used) - reserved)

    def db_room(self) -> int | None:
        return None if self.db_quota_bytes is None else max(0, self.db_quota_bytes - self.db_used_bytes)

    def storage_room(self) -> int | None:
        return None if self.storage_quota_bytes is None else max(0, self.storage_quota_bytes - self.storage_used_bytes)


def db_used_bytes(db, user_id: int) -> int:
    """Results plus kept raw answers (D-10.10, D-11.18)."""
    results = db.scalar(select(func.coalesce(func.sum(AnalysisResult.size_bytes), 0))
                        .where(AnalysisResult.user_id == user_id)) or 0
    raw = db.scalar(select(func.coalesce(func.sum(RawOutput.size_bytes), 0)).where(RawOutput.user_id == user_id)) or 0
    return results + raw


def storage_used_bytes(db, user_id: int) -> int:
    return db.scalar(
        select(func.coalesce(func.sum(Document.size_bytes), 0))
        .join(UserDocument, UserDocument.document_id == Document.id)
        .where(UserDocument.user_id == user_id, Document.storage_path.is_not(None))
    ) or 0


def usage_overview() -> dict[int, tuple[int, int]]:
    """Per user: (documents in the last 24 h incl. own keys, bytes used = results + kept PDFs) (D-12.14)."""
    overview: dict[int, list[int]] = {}
    with session_scope() as db:
        since = utcnow() - timedelta(days=1)
        for user_id, count in db.execute(select(UsageEvent.user_id, func.count()).where(UsageEvent.at >= since)
                                         .group_by(UsageEvent.user_id)):
            overview.setdefault(user_id, [0, 0])[0] = count
        for model in (AnalysisResult, RawOutput):
            for user_id, size in db.execute(select(model.user_id, func.sum(model.size_bytes)).group_by(model.user_id)):
                overview.setdefault(user_id, [0, 0])[1] += size or 0
        for user_id, size in db.execute(
                select(UserDocument.user_id, func.sum(Document.size_bytes))
                .join(Document, Document.id == UserDocument.document_id)
                .where(Document.storage_path.is_not(None)).group_by(UserDocument.user_id)):
            overview.setdefault(user_id, [0, 0])[1] += size or 0
    return {user_id: (docs, size) for user_id, (docs, size) in overview.items()}


def quota_status(user_id: int) -> QuotaStatus:
    with session_scope() as db:
        user = db.get(User, user_id)
        role = Role(user.role)
        now = utcnow()

        def counted(since=None, include_own=False) -> int:
            query = select(func.count()).select_from(UsageEvent).where(UsageEvent.user_id == user_id)
            if since is not None:
                query = query.where(UsageEvent.at >= since)
            if not include_own:
                query = query.where(UsageEvent.own_key.is_(False))
            return db.scalar(query) or 0

        unlimited = role is Role.ADMIN
        return QuotaStatus(
            role=role,
            hour_limit=user.docs_per_hour,
            hour_used=counted(now - timedelta(hours=1)) if role is Role.ANALYST else 0,
            day_limit=user.docs_per_day,
            day_used=counted(now - timedelta(days=1)) if role is Role.ANALYST else 0,
            guest_cap=user.guest_doc_cap,
            guest_used=counted(include_own=True) if role is Role.GUEST else 0,
            db_quota_bytes=None if unlimited else (user.db_quota_mb or 0) * MB,
            db_used_bytes=db_used_bytes(db, user_id),
            storage_quota_bytes=None if unlimited else (user.storage_quota_mb or 0) * MB,
            storage_used_bytes=storage_used_bytes(db, user_id),
        )
