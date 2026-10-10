"""History: request search, deletion, kept PDFs and sharing with viewers (D-10.9, D-12.2–D-12.6)."""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, func, select

from core.db import session_scope
from core.fa import TEHRAN_TZ
from core.models import (
    SHEET_TABLES,
    AnalysisRequest,
    Document,
    RequestDocument,
    RequestShare,
    Role,
    User,
    UserDocument,
    as_utc,
    utcnow,
)
from core.text_normalize import comparison_key
from services import results
from services.auth import CurrentUser
from services.jobs import RUNNING_STATUSES, STOPPED
from services.storage import get_storage

log = logging.getLogger(__name__)

# Request filters (D-12.3); "متوقف شده" covers interrupted and user-stopped requests (D-14.2)
RUNNING, COMPLETED, INTERRUPTED, HAS_FAILED = "running", "completed", "interrupted", "has_failed"
STATUS_FILTER_LABELS = {RUNNING: "در حال اجرا", COMPLETED: "تکمیل شده", INTERRUPTED: "متوقف شده",
                        HAS_FAILED: "دارای سند ناموفق"}
_OK = {"succeeded", "reused"}
_FAILED = {"failed", "interrupted"}


class HistoryError(ValueError):
    """Persian, user-facing error."""


@dataclass
class RequestRow:
    id: str
    user_id: int
    username: str
    created_at: datetime
    model_label: str
    status: str
    total: int = 0
    ok: int = 0
    failed: int = 0
    files: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status in RUNNING_STATUSES


def _day_start_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, TEHRAN_TZ).astimezone(UTC)


def search_requests(user: CurrentUser, *, term: str = "", since: date | None = None, until: date | None = None,
                    statuses: set[str] | frozenset[str] = frozenset(), model: str | None = None,
                    owner_id: int | None = None, shared: bool = False, limit: int = 500) -> list[RequestRow]:
    """Requests the user may see, newest first (D-12.3).

    `shared`: requests shared with this viewer. Otherwise the user's own requests (admins: everyone's,
    optionally one owner). Dates are Tehran calendar days, inclusive.
    """
    with session_scope() as db:
        query = select(AnalysisRequest, User.username).join(User, User.id == AnalysisRequest.user_id)
        if shared:
            query = query.join(RequestShare, RequestShare.request_id == AnalysisRequest.id).where(
                RequestShare.user_id == user.id)
        elif not user.is_admin:
            query = query.where(AnalysisRequest.user_id == user.id)
        elif owner_id is not None:
            query = query.where(AnalysisRequest.user_id == owner_id)
        if since is not None:
            query = query.where(AnalysisRequest.created_at >= _day_start_utc(since))
        if until is not None:
            query = query.where(AnalysisRequest.created_at < _day_start_utc(until + timedelta(days=1)))
        rows = [RequestRow(r.id, r.user_id, username, as_utc(r.created_at), f"{r.provider_name} › {r.model_name}",
                           r.status) for r, username in db.execute(query.order_by(AnalysisRequest.created_at.desc()))]
        if model:
            rows = [r for r in rows if r.model_label == model]
        if not rows:
            return []
        by_id = {r.id: r for r in rows}
        docs = db.execute(select(RequestDocument.request_id, RequestDocument.document_id, RequestDocument.file_name,
                                 RequestDocument.status, RequestDocument.result_request_id)
                          .where(RequestDocument.request_id.in_(by_id)))
        wanted: dict[tuple[str, str], list[RequestRow]] = {}
        for request_id, document_id, file_name, status, result_request_id in docs:
            row = by_id[request_id]
            row.total += 1
            row.ok += status in _OK
            row.failed += status in _FAILED
            row.files.append(file_name)
            if result_request_id:
                wanted.setdefault((result_request_id, document_id), []).append(row)
        if wanted:
            summary = SHEET_TABLES["summary"]
            for request_id, document_id, company in db.execute(
                    select(summary.c.request_id, summary.c.document_id, summary.c.company_name)
                    .where(summary.c.request_id.in_({k[0] for k in wanted}))):
                for row in wanted.get((request_id, document_id), []):
                    if company:
                        row.companies.append(company)

    def status_ok(row: RequestRow) -> bool:
        if not statuses:
            return True
        return ((RUNNING in statuses and row.running) or (COMPLETED in statuses and row.status == "completed")
                or (INTERRUPTED in statuses and row.status in (INTERRUPTED, STOPPED))
                or (HAS_FAILED in statuses and row.failed > 0))

    key = comparison_key(term) if term.strip() else ""

    def text_ok(row: RequestRow) -> bool:
        if not key:
            return True
        return any(key in comparison_key(value) for value in (row.id, *row.files, *row.companies))

    return [r for r in rows if status_ok(r) and text_ok(r)][:limit]


# ---------------------------------------------------------------- deleting requests (D-10.9, D-12.5)
def delete_request(user: CurrentUser, request_id: str, *, running: bool) -> None:
    """Delete a finished request. Results still reused by another request are kept until that one goes."""
    if running:
        raise HistoryError("درخواست در حال اجرا قابل حذف نیست؛ پس از پایان آن دوباره تلاش کنید.")
    with session_scope() as db:
        request = db.get(AnalysisRequest, request_id)
        if request is None:
            raise HistoryError("درخواست یافت نشد.")
        if request.user_id != user.id and not user.is_admin:
            raise HistoryError("فقط صاحب درخواست یا مدیر سامانه می‌تواند آن را حذف کند.")
        rows = db.scalars(select(RequestDocument).where(RequestDocument.request_id == request_id)).all()
        referenced = {(r.result_request_id, r.document_id) for r in rows if r.result_request_id}
        referenced |= {(request_id, r.document_id) for r in rows}  # own results and kept raw answers
        db.execute(delete(RequestShare).where(RequestShare.request_id == request_id))
        db.execute(delete(RequestDocument).where(RequestDocument.request_id == request_id))
        db.delete(request)
        db.flush()
        for result_request_id, document_id in referenced:
            still_used = db.scalar(select(func.count()).select_from(RequestDocument).where(
                RequestDocument.result_request_id == result_request_id, RequestDocument.document_id == document_id))
            if not still_used:
                results.delete_result(db, result_request_id, document_id)
    log.info("Request %s deleted by %s", request_id, user.username)


# ---------------------------------------------------------------- kept PDFs (D-12.4)
@dataclass(frozen=True)
class KeptFile:
    user_id: int
    username: str
    document_id: str
    file_name: str
    size_bytes: int
    created_at: datetime
    requests: int


def kept_files(user: CurrentUser) -> list[KeptFile]:
    """The user's kept PDFs (admins: everyone's), newest first."""
    with session_scope() as db:
        uses = (select(AnalysisRequest.user_id.label("user_id"), RequestDocument.document_id.label("document_id"),
                       func.count(func.distinct(RequestDocument.request_id)).label("n"))
                .join(AnalysisRequest, AnalysisRequest.id == RequestDocument.request_id)
                .group_by(AnalysisRequest.user_id, RequestDocument.document_id).subquery())
        query = (select(UserDocument, User.username, Document.size_bytes, func.coalesce(uses.c.n, 0))
                 .join(User, User.id == UserDocument.user_id)
                 .join(Document, Document.id == UserDocument.document_id)
                 .outerjoin(uses, (uses.c.user_id == UserDocument.user_id)
                            & (uses.c.document_id == UserDocument.document_id))
                 .where(Document.storage_path.is_not(None))
                 .order_by(UserDocument.created_at.desc()))
        if not user.is_admin:
            query = query.where(UserDocument.user_id == user.id)
        return [KeptFile(ud.user_id, username, ud.document_id, ud.file_name, size, as_utc(ud.created_at), n)
                for ud, username, size, n in db.execute(query)]


def delete_kept_files(user: CurrentUser, items: list[tuple[int, str]]) -> int:
    """Stop keeping PDFs (user_id, document_id); results stay. A file shared by nobody is removed physically."""
    removed_paths = []
    with session_scope() as db:
        for owner_id, document_id in items:
            if owner_id != user.id and not user.is_admin:
                raise HistoryError("فقط فایل‌های خودتان را می‌توانید حذف کنید.")
            kept = db.get(UserDocument, (owner_id, document_id))
            if kept is None:
                continue
            db.delete(kept)
            db.flush()
            others = db.scalar(select(func.count()).select_from(UserDocument)
                               .where(UserDocument.document_id == document_id))
            document = db.get(Document, document_id)
            if not others and document is not None and document.storage_path:
                removed_paths.append(document.storage_path)
                document.storage_path = None
    for path in removed_paths:
        try:
            get_storage().delete(path)
        except Exception:  # noqa: BLE001 - the DB no longer points to it; a stray object only costs space
            log.exception("Could not delete stored PDF %s", path)
    log.info("%d kept PDF(s) removed by %s (%d deleted from storage)", len(items), user.username, len(removed_paths))
    return len(items)


def kept_pdfs(user: CurrentUser, items: list[tuple[int, str, str]]) -> tuple[bytes, str, str]:
    """Kept PDFs (user_id, document_id, file name) for download (D-14.10): (data, file name, mime).

    One file as is, several as a ZIP. Only the user's own files (admins: anyone's).
    """
    files = []
    with session_scope() as db:
        for owner_id, document_id, name in items:
            if owner_id != user.id and not user.is_admin:
                raise HistoryError("فقط فایل‌های خودتان را می‌توانید دانلود کنید.")
            document = db.get(Document, document_id)
            if document is not None and document.storage_path:
                files.append((name, document.storage_path))
    loaded = [(name, data) for name, path in files if (data := get_storage().get(path)) is not None]
    if len(loaded) == 1:
        return loaded[0][1], loaded[0][0], "application/pdf"
    buffer, used = io.BytesIO(), set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in loaded:
            stem, dot, ext = name.rpartition(".")
            unique, n = name, 1
            while unique in used:  # same name uploaded twice with different content
                n += 1
                unique = f"{stem or ext} ({n}){dot}{ext if stem else ''}"
            used.add(unique)
            archive.writestr(unique, data)
    return buffer.getvalue(), "kept_files.zip", "application/zip"


# ---------------------------------------------------------------- sharing (D-12.6, D-14.7)
SHARE_ROLES = (Role.ANALYST, Role.VIEWER)


@dataclass(frozen=True)
class ShareOption:
    id: int
    username: str
    display_name: str
    role: Role


def share_options(owner_id: int | None = None) -> list[ShareOption]:
    """Active, unexpired analysts and viewers a request can be shared with (never its owner)."""
    now = utcnow()
    with session_scope() as db:
        users = db.scalars(select(User).where(User.role.in_([r.value for r in SHARE_ROLES]),
                                              User.is_active.is_(True)).order_by(User.role, User.username))
        return [ShareOption(u.id, u.username, u.display_name, Role(u.role)) for u in users
                if not u.is_expired(now) and u.id != owner_id]


def shared_with(request_id: str) -> list[int]:
    with session_scope() as db:
        return list(db.scalars(select(RequestShare.user_id).where(RequestShare.request_id == request_id)))


def set_shares(user: CurrentUser, request_id: str, viewer_ids: list[int]) -> None:
    """Replace the analysts and viewers a request is shared with (owner or admin)."""
    with session_scope() as db:
        request = db.get(AnalysisRequest, request_id)
        if request is None:
            raise HistoryError("درخواست یافت نشد.")
        if request.user_id != user.id and not user.is_admin:
            raise HistoryError("فقط صاحب درخواست یا مدیر سامانه می‌تواند آن را به اشتراک بگذارد.")
        wanted = set(viewer_ids)
        if wanted - {o.id for o in share_options(request.user_id)}:
            raise HistoryError("فقط با حساب‌های فعال تحلیلگر یا مشاهده‌گر (به‌جز صاحب درخواست) می‌توان اشتراک‌گذاری کرد.")
        current = set(db.scalars(select(RequestShare.user_id).where(RequestShare.request_id == request_id)))
        if current - wanted:
            db.execute(delete(RequestShare).where(RequestShare.request_id == request_id,
                                                  RequestShare.user_id.in_(current - wanted)))
        for viewer_id in wanted - current:
            db.add(RequestShare(request_id=request_id, user_id=viewer_id, shared_by=user.id))
    log.info("Request %s shared by %s with %d user(s)", request_id, user.username, len(set(viewer_ids)))
