"""Background analysis engine (D-5.3..5.7, D-10.2, D-10.3, D-11.10, D-4.11).

* A request is submitted, its documents are planned (duplicates, cached results, size limits,
  quota) and spooled to disk, then queued in the process-wide scheduler (services/scheduler.py),
  which decides when each document may call which key.
* Every document is processed in isolation: key rotation, rate limiting, smart retries; one
  failure never affects the others.
* Live state is kept in memory; requests of non-guests are persisted to the DB as they progress.
  Guest requests and results that could not be stored stay in memory for the guest TTL.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from core.config import MAX_PDF_MB, PROJECT_ROOT, RETRY_BACKOFF_S, get_settings
from core.db import session_scope
from core.fa import fa_digits
from core.models import (
    AnalysisRequest,
    AnalysisResult,
    Document,
    RequestDocument,
    RequestShare,
    Role,
    UsageEvent,
    User,
    UserDocument,
    as_utc,
    utcnow,
)
from services import results
from services.auth import CurrentUser
from services.llm import adapter_for
from services.llm.base import AnalysisOutput
from services.providers import KeyRuntime, ModelChoice, ProviderError, record_key_error, runtime_keys, usable_models
from services.quota import MB, db_used_bytes, quota_status
from services.scheduler import Run, Scheduler, Task
from services.storage import get_storage, object_path
from services.uploads import IncomingFile

log = logging.getLogger(__name__)

# Document statuses
QUEUED, PROCESSING, SUCCEEDED, FAILED = "queued", "processing", "succeeded", "failed"
DUPLICATE, SKIPPED_QUOTA, REUSED, INTERRUPTED = "duplicate", "skipped_quota", "reused", "interrupted"
STOPPED = "stopped"  # stopped by the user before reaching the model (D-14.2)
FINAL = {SUCCEEDED, FAILED, DUPLICATE, SKIPPED_QUOTA, REUSED, INTERRUPTED, STOPPED}
RETRYABLE_STATUSES = {FAILED, INTERRUPTED, SKIPPED_QUOTA, STOPPED}
# Request statuses
RUNNING, STOPPING, COMPLETED = "running", "stopping", "completed"
RUNNING_STATUSES = (QUEUED, RUNNING, STOPPING)

STATUS_LABELS = {
    QUEUED: "در صف",
    PROCESSING: "در حال تحلیل",
    SUCCEEDED: "موفق",
    FAILED: "ناموفق",
    DUPLICATE: "تکراری – نادیده گرفته شد",
    SKIPPED_QUOTA: "رد شده – سهمیه",
    REUSED: "از نتایج قبلی",
    INTERRUPTED: "متوقف شده",
    STOPPED: "متوقف‌شده توسط کاربر",
}
REQUEST_STATUS_LABELS = {QUEUED: "در صف", RUNNING: "در حال اجرا", COMPLETED: "تکمیل شده",
                         INTERRUPTED: "متوقف شده", STOPPING: "در حال توقف", STOPPED: "متوقف شده (توسط کاربر)"}
STOPPED_REASON = "به درخواست کاربر متوقف شد؛ با «اجرای مجدد» ادامه دهید."

SPOOL_KEEP_DAYS = 7


class JobError(ValueError):
    """Submission/retry error with a Persian, user-facing message."""


@dataclass
class DocState:
    position: int
    file_name: str
    document_id: str
    size: int
    status: str
    reason: str | None = None
    attempts: int = 0
    result_request_id: str | None = None
    stored: bool = False
    keep_pdf: bool = False
    temperature_applied: bool | None = None
    input_tokens: int | None = None  # last attempt that got an answer (D-14.6)
    output_tokens: int | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    last_call_seconds: float | None = None  # model time (D-14.5)
    total_call_seconds: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: dict | None = None
    note: str | None = None  # informational, shown with the final status (e.g. PDF not kept)
    source_model: str | None = None  # "provider › model" of a reused result (D-12.1); None = the request's model
    warnings: list[dict] | None = None  # D-11.18: {"code", "message", ...}
    data: dict | None = field(default=None, repr=False)  # only for results that are not stored in the DB
    raw_answers: list[dict] = field(default_factory=list, repr=False)  # kept answers not stored in the DB
    row_id: int | None = None


@dataclass
class RequestState:
    id: str
    user_id: int
    role: Role
    model_id: int | None
    provider_name: str
    model_name: str
    api_type: str
    key_ids: list[int]
    own_keys: bool
    persist: bool
    username: str = ""  # creator, shown to admins (Req. 2)
    parent_request_id: str | None = None  # re-run of documents of another request (D-14.3)
    status: str = QUEUED
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    docs: list[DocState] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def count(self, *statuses: str) -> int:
        return sum(1 for d in self.docs if d.status in statuses)

    @property
    def model_label(self) -> str:
        return f"{self.provider_name} › {self.model_name}"

    @property
    def is_running(self) -> bool:
        return self.status in RUNNING_STATUSES


def doc_status_label(doc: DocState) -> str:
    label = STATUS_LABELS.get(doc.status, doc.status)
    return f"{label} – با هشدار" if doc.warnings and doc.status == SUCCEEDED else label


def doc_explanation(doc: DocState) -> str:
    """Reason plus any warning not already part of it (D-11.18)."""
    parts = [doc.reason] if doc.reason else []
    parts += [w["message"] for w in doc.warnings or [] if not doc.reason or w["message"] not in doc.reason]
    return " ".join(parts)


@dataclass(frozen=True)
class Version:
    """A stored result of a document that may be reused (D-5.6, D-12.1)."""

    request_id: str
    document_id: str
    user_id: int
    username: str
    provider_name: str
    model_name: str
    created_at: datetime
    has_warnings: bool


def order_versions(versions: list[Version], provider_name: str, model_name: str) -> list[Version]:
    """Same provider+model first, then the others; newest first within each group (D-12.1)."""
    def same(v: Version) -> bool:
        return v.provider_name == provider_name and v.model_name == model_name

    newest = sorted(versions, key=lambda v: v.created_at, reverse=True)
    return [v for v in newest if same(v)] + [v for v in newest if not same(v)]


def default_version(versions: list[Version], provider_name: str, model_name: str) -> str | None:
    """Request id of the newest same-model result, or None for a new analysis (D-5.6)."""
    ordered = order_versions(versions, provider_name, model_name)
    if ordered and (ordered[0].provider_name, ordered[0].model_name) == (provider_name, model_name):
        return ordered[0].request_id
    return None


def _source_model(db, request, result_request_id: str | None, document_id: str) -> str | None:
    """Label of a reused result's model when it differs from the request's (D-12.1)."""
    if not result_request_id or result_request_id == request.id:
        return None
    result = db.get(AnalysisResult, (result_request_id, document_id))
    if result is None or (result.provider_name, result.model_name) == (request.provider_name, request.model_name):
        return None
    return f"{result.provider_name} › {result.model_name}"


def new_request_id() -> str:
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return f"R-{datetime.now(UTC):%Y%m%d}-" + "".join(secrets.choice(alphabet) for _ in range(4))


class _EngineHandler:
    """Connects the scheduler's steps to the JobManager (status, persistence, results)."""

    def __init__(self, manager: JobManager):
        self.m = manager

    def started(self, task: Task, key: KeyRuntime) -> None:
        doc = task.doc
        self.m._set(task.run.state, doc, status=PROCESSING, started_at=doc.started_at or utcnow(),
                    attempts=task.attempts, reason=None)

    def call(self, task: Task, key: KeyRuntime) -> AnalysisOutput:
        state, doc = task.run.state, task.doc
        pdf = self.m._spool_file(state.id, doc.document_id).read_bytes()
        return adapter_for(state.api_type).analyze_pdf(key.cfg, pdf, doc.file_name)

    def succeeded(self, task: Task, output: AnalysisOutput) -> None:
        self.m._complete(task.run.state, task.doc, output)

    def failed(self, task: Task, reason: str) -> None:
        state, doc = task.run.state, task.doc
        self.m._set(state, doc, status=FAILED, finished_at=utcnow(), attempts=task.attempts, reason=reason)
        self.m._keep_pdf(state, doc)

    def attempt_done(self, task: Task, seconds: float, input_tokens: int | None, output_tokens: int | None,
                     answered: bool) -> None:
        """Model time and tokens of one attempt (D-14.5, D-14.6); persisted with the next status change."""
        doc = task.doc
        with task.run.state.lock:
            doc.last_call_seconds = round(seconds, 1)
            doc.total_call_seconds = round(doc.total_call_seconds + seconds, 1)
            if answered:
                doc.input_tokens, doc.output_tokens = input_tokens, output_tokens
                doc.total_input_tokens += input_tokens or 0
                doc.total_output_tokens += output_tokens or 0

    def stopped(self, task: Task) -> None:
        state, doc = task.run.state, task.doc
        self.m._set(state, doc, status=STOPPED, finished_at=utcnow(), attempts=task.attempts, reason=STOPPED_REASON)
        self.m._keep_pdf(state, doc)

    def retry_wait(self, task: Task, reason: str) -> None:
        self.m._set(task.run.state, task.doc, status=QUEUED, attempts=task.attempts, reason=reason)

    def waiting(self, task: Task, reason: str) -> None:
        state = task.run.state
        with state.lock:  # shown live on the page; not worth a database write (D-11.13)
            task.doc.reason = reason

    def key_error(self, key_id: int, message: str) -> None:
        record_key_error(key_id, message)

    def rejected(self, task: Task, error) -> None:
        self.m._keep_answer(task.run.state, task.doc, task.attempts, "rejected", str(error), error.raw_text,
                            error.input_tokens, error.output_tokens)

    def request_done(self, run: Run) -> None:
        self.m._finish(run.state)


class JobManager:
    def __init__(self, spool_dir: Path | None = None, backoff: tuple[float, ...] = RETRY_BACKOFF_S):
        self._active: dict[str, RequestState] = {}
        self._ephemeral: dict[str, tuple[RequestState, float]] = {}
        self._lock = threading.RLock()
        self._user_locks: dict[int, threading.Lock] = {}
        self._done: dict[str, threading.Event] = {}
        self.spool_dir = spool_dir or PROJECT_ROOT / "data" / "spool"
        self.scheduler = Scheduler(_EngineHandler(self), backoff=backoff)

    def close(self) -> None:
        """Stop the scheduler thread (tests and scripts)."""
        self.scheduler.stop()

    # ---------------------------------------------------------------- helpers
    def _user_lock(self, user_id: int) -> threading.Lock:
        with self._lock:
            return self._user_locks.setdefault(user_id, threading.Lock())

    def _spool_file(self, request_id: str, document_id: str) -> Path:
        return self.spool_dir / request_id / f"{document_id}.pdf"

    def _ttl_seconds(self) -> float:
        return get_settings().guest_result_ttl_minutes * 60

    def reserved(self, user_id: int, own_keys: bool) -> int:
        """Documents of the user's running requests that may still turn into counted analyses."""
        with self._lock:
            states = [s for s in self._active.values() if s.user_id == user_id and s.is_running]
        return sum(s.count(QUEUED, PROCESSING) for s in states if s.own_keys == own_keys or s.role is Role.GUEST)

    def _purge_ephemeral(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [rid for rid, (_, exp) in self._ephemeral.items() if exp < now]
            for rid in expired:
                state, _ = self._ephemeral.pop(rid)
                if not state.persist:
                    shutil.rmtree(self.spool_dir / rid, ignore_errors=True)

    # ---------------------------------------------------------------- persistence
    def _persist_new(self, state: RequestState, model_choice: ModelChoice) -> None:
        with session_scope() as db:
            db.add(AnalysisRequest(
                id=state.id, user_id=state.user_id, provider_id=model_choice.provider_id, model_id=model_choice.model_id,
                provider_name=state.provider_name, model_name=state.model_name, api_type=state.api_type,
                api_key_ids=state.key_ids, own_keys=state.own_keys, status=state.status, created_at=state.created_at,
                parent_request_id=state.parent_request_id,
            ))
            for doc in state.docs:
                if db.get(Document, doc.document_id) is None:
                    db.add(Document(id=doc.document_id, size_bytes=doc.size))
                    db.flush()
                row = RequestDocument(
                    request_id=state.id, document_id=doc.document_id, position=doc.position, file_name=doc.file_name,
                    status=doc.status, reason=doc.reason, result_request_id=doc.result_request_id, stored=doc.stored,
                    finished_at=doc.finished_at,
                )
                db.add(row)
                db.flush()
                doc.row_id = row.id

    def _save_doc(self, state: RequestState, doc: DocState) -> None:
        if not state.persist or doc.row_id is None:
            return
        with session_scope() as db:
            db.execute(update(RequestDocument).where(RequestDocument.id == doc.row_id).values(
                status=doc.status, reason=doc.reason, attempts=doc.attempts, result_request_id=doc.result_request_id,
                stored=doc.stored, temperature_applied=doc.temperature_applied, input_tokens=doc.input_tokens,
                output_tokens=doc.output_tokens, total_input_tokens=doc.total_input_tokens,
                total_output_tokens=doc.total_output_tokens, last_call_seconds=doc.last_call_seconds,
                total_call_seconds=doc.total_call_seconds, started_at=doc.started_at, finished_at=doc.finished_at,
                warnings=doc.warnings,
            ))

    def _save_request(self, state: RequestState) -> None:
        if not state.persist:
            return
        with session_scope() as db:
            db.execute(update(AnalysisRequest).where(AnalysisRequest.id == state.id).values(
                status=state.status, finished_at=state.finished_at, api_key_ids=state.key_ids))

    def _set(self, state: RequestState, doc: DocState, **changes) -> None:
        with state.lock:
            for name, value in changes.items():
                setattr(doc, name, value)
        self._save_doc(state, doc)

    # ---------------------------------------------------------------- previous versions (D-5.6)
    @staticmethod
    def previous_versions(user: CurrentUser, document_ids: list[str]) -> dict[str, list[Version]]:
        """Stored results per document; only the user's own (admin: everyone's), newest first."""
        if not document_ids or user.role is Role.GUEST:
            return {}
        with session_scope() as db:
            query = (select(AnalysisResult, User.username).join(User, User.id == AnalysisResult.user_id)
                     .where(AnalysisResult.document_id.in_(document_ids)))
            if not user.is_admin:
                query = query.where(AnalysisResult.user_id == user.id)
            found: dict[str, list[Version]] = {}
            for result, username in db.execute(query.order_by(AnalysisResult.created_at.desc())):
                found.setdefault(result.document_id, []).append(Version(
                    result.request_id, result.document_id, result.user_id, username, result.provider_name,
                    result.model_name, as_utc(result.created_at), bool(result.warnings)))
            return found

    # ---------------------------------------------------------------- submission
    def submit(self, user: CurrentUser, choice: ModelChoice, key_ids: list[int], files: list[IncomingFile],
               reuse: dict[str, str] | None = None, parent_request_id: str | None = None) -> str:
        """Plan and start a request. `reuse` maps document_id -> request_id of a stored result to reuse."""
        if user.role is Role.VIEWER:
            raise JobError("نقش مشاهده‌گر امکان تحلیل ندارد.")
        if not files:
            raise JobError("هیچ فایلی برای تحلیل انتخاب نشده است.")
        allowed = {k.id for k in choice.keys}
        key_ids = [k for k in key_ids if k in allowed]
        if not key_ids:
            raise JobError("حداقل یک کلید API مجاز انتخاب کنید.")
        persist = user.role is not Role.GUEST
        reuse = (reuse or {}) if persist else {}
        self._purge_ephemeral()

        with self._user_lock(user.id):
            state = RequestState(
                id=new_request_id(), user_id=user.id, role=user.role, model_id=choice.model_id,
                provider_name=choice.provider_name, model_name=choice.model_name, api_type=choice.api_type,
                key_ids=key_ids, own_keys=choice.own, persist=persist, username=user.username,
                parent_request_id=parent_request_id,
            )
            limit_bytes = min(MAX_PDF_MB, choice.max_file_mb or MAX_PDF_MB) * MB
            seen: dict[str, str] = {}
            spool: dict[str, bytes] = {}
            for position, item in enumerate(files):
                sha = item.sha256
                doc = DocState(position, item.name, sha, item.size, QUEUED)
                if sha in seen:
                    doc.status, doc.reason = DUPLICATE, f"محتوای این فایل با «{seen[sha]}» یکسان است."
                elif reuse.get(sha) and self._may_reuse(user, reuse[sha], sha):
                    doc.status, doc.result_request_id, doc.stored = REUSED, reuse[sha], True
                    with session_scope() as db:
                        doc.summary = results.summary_row(db, reuse[sha], sha)
                        doc.source_model = _source_model(db, state, reuse[sha], sha)
                elif item.size > limit_bytes:
                    doc.status = FAILED
                    doc.reason = f"حجم فایل از سقف مجاز این مدل ({fa_digits(limit_bytes // MB)} مگابایت) بیشتر است."
                if doc.status in (REUSED, DUPLICATE, FAILED):
                    doc.finished_at = utcnow()
                if doc.status != DUPLICATE:
                    seen[sha] = item.name
                    spool[sha] = item.data
                state.docs.append(doc)

            # Quota (D-4.4/D-5.5): process what fits, skip the rest
            status = quota_status(user.id)
            remaining = status.remaining_docs(choice.own, reserved=self.reserved(user.id, choice.own))
            new_docs = [d for d in state.docs if d.status == QUEUED]
            for index, doc in enumerate(new_docs):
                if remaining is not None and index >= remaining:
                    doc.status, doc.finished_at = SKIPPED_QUOTA, utcnow()
                    doc.reason = "سهمیه تحلیل شما در این بازه زمانی پر است؛ بعداً از «ادامه یا اجرای مجدد» استفاده کنید."

            # Keep PDFs while storage capacity allows (D-4.5: never block)
            if persist:
                room = status.storage_room()
                with session_scope() as db:
                    kept = set(db.scalars(select(UserDocument.document_id).where(UserDocument.user_id == user.id)))
                for doc in state.docs:
                    if doc.status == DUPLICATE or doc.document_id in kept:
                        continue
                    if room is None or doc.size <= room:
                        doc.keep_pdf = True
                        room = None if room is None else room - doc.size
                    elif doc.status == QUEUED:
                        doc.note = "فضای ذخیره فایل شما پر است؛ فایل PDF نگه‌داری نشد."

            for sha, data in spool.items():
                target = self._spool_file(state.id, sha)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            if persist:
                self._persist_new(state, choice)
            with self._lock:
                self._active[state.id] = state
        self._start(state, [d for d in state.docs if d.status == QUEUED])
        return state.id

    @staticmethod
    def _may_reuse(user: CurrentUser, request_id: str, document_id: str) -> bool:
        """A result may be reused by its owner (admins: anyone's, D-5.6)."""
        with session_scope() as db:
            result = db.get(AnalysisResult, (request_id, document_id))
            return result is not None and (user.is_admin or result.user_id == user.id)

    def _start(self, state: RequestState, docs: list[DocState]) -> None:
        """Hand the queued documents to the scheduler (or fail them if the model/keys are unusable)."""
        with self._lock:
            self._done[state.id] = threading.Event()
        state.status = RUNNING
        self._save_request(state)
        if not docs:
            self._finish(state)
            return
        try:
            model, _provider, keys = runtime_keys(state.model_id, state.key_ids)
        except (ProviderError, Exception) as exc:  # noqa: BLE001 - model deleted, key undecryptable...
            log.exception("Cannot load model/keys for %s", state.id)
            self._fail_all(state, docs, f"تنظیمات مدل یا کلید قابل بارگذاری نیست: {exc}")
            return
        if not keys:
            self._fail_all(state, docs, "هیچ کلید فعالی برای این درخواست باقی نمانده است.")
            return
        self.scheduler.add_run(
            request_id=state.id, user_id=state.user_id, username=state.username, model_id=model.id,
            model_label=f"{state.provider_name} › {model.display_name}", max_attempts=model.max_attempts,
            keys=keys, state=state, docs=docs,
        )

    def _fail_all(self, state: RequestState, docs: list[DocState], reason: str) -> None:
        for doc in docs:
            self._set(state, doc, status=FAILED, reason=reason, finished_at=utcnow())
            self._keep_pdf(state, doc)
        self._finish(state)

    def _keep_pdf(self, state: RequestState, doc: DocState) -> None:
        """Copy a finished document's PDF to storage if it is to be kept (D-4.5, D-11.13)."""
        if not (state.persist and doc.keep_pdf):
            return
        try:
            with session_scope() as db:
                document = db.get(Document, doc.document_id)
                if document.storage_path is None:
                    source = self._spool_file(state.id, doc.document_id)
                    path = object_path(doc.document_id)
                    get_storage().put(path, source.read_bytes())
                    document.storage_path = path
                if db.get(UserDocument, (state.user_id, doc.document_id)) is None:
                    db.add(UserDocument(user_id=state.user_id, document_id=doc.document_id, file_name=doc.file_name))
        except Exception:  # noqa: BLE001 - storage problems must not affect the analysis
            log.exception("Could not keep PDF %s", doc.document_id)
            doc.keep_pdf = False

    def _db_room(self, db, state: RequestState) -> int | None:
        """Bytes left in the user's results quota (None = unlimited)."""
        if state.role is Role.ADMIN:
            return None
        return max(0, quota_status(state.user_id).db_quota_bytes - db_used_bytes(db, state.user_id))

    def _keep_answer(self, state: RequestState, doc: DocState, attempt: int, kind: str, reason: str | None,
                     text: str, input_tokens: int | None, output_tokens: int | None) -> None:
        """Keep a model answer that wasn't a clean JSON, verbatim (D-11.18): in the DB, else in memory."""
        stored = False
        if state.persist:
            try:
                with self._user_lock(state.user_id), session_scope() as db:
                    room = self._db_room(db, state)
                    if room is None or len(results.gzip_text(text)) <= room:
                        results.save_raw_output(db, request_id=state.id, document_id=doc.document_id,
                                                user_id=state.user_id, attempt=attempt, kind=kind, reason=reason,
                                                model_name=state.model_name, text=text, input_tokens=input_tokens,
                                                output_tokens=output_tokens)
                        stored = True
            except Exception:  # noqa: BLE001 - fall back to memory
                log.exception("Could not store the raw answer of %s/%s", state.id, doc.file_name)
        if not stored:
            with state.lock:
                doc.raw_answers.append({"attempt": attempt, "kind": kind, "reason": reason, "text": text,
                                        "created_at": utcnow()})
        if kind == "rejected":
            warning = {"code": "rejected_answer_kept", "attempt": attempt,
                       "message": f"پاسخ مدل در تلاش {fa_digits(attempt)} قابل استفاده نبود؛ متن کامل آن نگه‌داری شد."}
            self._set(state, doc, warnings=[*(doc.warnings or []), warning])
        log.warning("Kept %s answer of %s/%s attempt %d (%d characters, %s)", kind, state.id, doc.file_name,
                    attempt, len(text), "database" if stored else "memory")

    def raw_answers(self, state: RequestState, doc: DocState) -> list[dict]:
        """Kept answers of a document, oldest first (from the DB and from memory)."""
        found = list(doc.raw_answers)
        if state.persist:
            with session_scope() as db:
                found += [{"attempt": r.attempt, "kind": r.kind, "reason": r.reason, "created_at": as_utc(r.created_at),
                           "text": results.gunzip_text(r.raw_text_gz)}
                          for r in results.raw_outputs(db, state.id, doc.document_id)]
        return sorted(found, key=lambda a: (a["attempt"], a["created_at"]))

    def _complete(self, state: RequestState, doc: DocState, output) -> None:
        prepared = results.prepare(output.data)
        summary = results.summary_from_data(output.data)
        if prepared.unexpected:
            log.warning("Unexpected item shape in %s/%s (kept in 'extra' and the raw JSON): %s",
                        state.id, doc.file_name, "; ".join(prepared.unexpected)[:500])
        warnings = list(output.warnings)
        raw_size = len(results.gzip_text(output.raw_text)) if output.raw_text is not None else 0
        stored, reason = False, None
        if state.persist:
            try:
                with self._user_lock(state.user_id), session_scope() as db:
                    room = self._db_room(db, state)
                    if room is None or prepared.size_bytes + raw_size <= room:
                        results.save_result(
                            db, request_id=state.id, document_id=doc.document_id, user_id=state.user_id,
                            provider_name=state.provider_name, model_name=state.model_name, api_type=state.api_type,
                            temperature_applied=output.temperature_applied, input_tokens=output.input_tokens,
                            output_tokens=output.output_tokens, prepared=prepared, warnings=warnings,
                        )
                        if output.raw_text is not None:  # accepted with a warning: keep the whole answer
                            results.save_raw_output(
                                db, request_id=state.id, document_id=doc.document_id, user_id=state.user_id,
                                attempt=doc.attempts, kind="accepted", reason=None, model_name=state.model_name,
                                text=output.raw_text, input_tokens=output.input_tokens,
                                output_tokens=output.output_tokens)
                        stored = True
                    else:
                        reason = "سهمیه ذخیره نتایج شما پر است؛ این نتیجه ذخیره نشد و فقط به‌صورت خروجی اکسل در دسترس است."
            except Exception:  # noqa: BLE001 - a paid analysis is never thrown away (D-11.16)
                log.exception("Saving the result of %s/%s failed; kept in memory", state.id, doc.file_name)
                reason = ("ذخیره نتیجه در پایگاه داده با خطا مواجه شد؛ نتیجه تحلیل از دست نرفته و به‌صورت موقت "
                          "در دسترس است (مانند حساب مهمان). جزئیات در گزارش رویدادهای سرور ثبت شد.")
        with session_scope() as db:  # counted even when the result could not be stored
            db.add(UsageEvent(user_id=state.user_id, request_id=state.id, document_id=doc.document_id,
                              own_key=state.own_keys, input_tokens=output.input_tokens,
                              output_tokens=output.output_tokens))
        keep_in_memory = not stored
        if output.raw_text is not None and not stored:
            with state.lock:
                doc.raw_answers.append({"attempt": doc.attempts, "kind": "accepted", "reason": None,
                                        "text": output.raw_text, "created_at": utcnow()})
        if warnings:
            log.warning("Result of %s/%s accepted with warnings: %s", state.id, doc.file_name,
                        [w["code"] for w in warnings])
        reason = " ".join(part for part in (*(w["message"] for w in warnings), reason, doc.note) if part) or None
        self._set(state, doc, status=SUCCEEDED, stored=stored, result_request_id=state.id if stored else None,
                  temperature_applied=output.temperature_applied, input_tokens=output.input_tokens,
                  output_tokens=output.output_tokens, finished_at=utcnow(), summary=summary, reason=reason,
                  data=output.data if keep_in_memory else None, warnings=[*(doc.warnings or []), *warnings] or None)
        self._keep_pdf(state, doc)
        self._spool_file(state.id, doc.document_id).unlink(missing_ok=True)

    def _finish(self, state: RequestState) -> None:
        with state.lock:
            for doc in state.docs:
                if doc.status not in FINAL:  # should not happen; never leave a document hanging
                    doc.status, doc.finished_at = FAILED, utcnow()
                    doc.reason = doc.reason or "پردازش این سند کامل نشد."
            state.status = STOPPED if state.status == STOPPING else COMPLETED
            state.finished_at = utcnow()
        self._save_request(state)
        with self._lock:
            self._active.pop(state.id, None)
            done = self._done.pop(state.id, None)
            if not state.persist or any(d.data is not None for d in state.docs):
                if state.role is Role.GUEST:  # a guest keeps only the last request (D-4.11)
                    for rid in [r for r, (s, _) in self._ephemeral.items() if s.user_id == state.user_id]:
                        self._ephemeral.pop(rid)
                self._ephemeral[state.id] = (state, time.monotonic() + self._ttl_seconds())
        if state.persist and not any(d.status in RETRYABLE_STATUSES for d in state.docs):
            shutil.rmtree(self.spool_dir / state.id, ignore_errors=True)
        if done is not None:
            done.set()

    def wait_idle(self, request_id: str, timeout: float = 60) -> bool:
        """Block until every document of the request is final (tests and scripts)."""
        with self._lock:
            done = self._done.get(request_id)
        return done.wait(timeout) if done is not None else True

    # ---------------------------------------------------------------- reading
    def get(self, request_id: str, user: CurrentUser) -> RequestState | None:
        self._purge_ephemeral()
        with self._lock:
            state = self._active.get(request_id) or (self._ephemeral.get(request_id) or (None,))[0]
        if state is None:
            state = self._load(request_id)
        if state is None or not self.may_view(state, user):
            return None
        return state

    @staticmethod
    def may_view(state: RequestState, user: CurrentUser) -> bool:
        """Owner and admins; analysts and viewers when the request is shared with them (D-12.6, D-14.7)."""
        if state.user_id == user.id or user.is_admin:
            return True
        if user.role not in (Role.VIEWER, Role.ANALYST) or not state.persist:
            return False
        with session_scope() as db:
            return db.get(RequestShare, (state.id, user.id)) is not None

    def expires_at(self, request_id: str) -> datetime | None:
        """When a request kept only in memory disappears (guests, unsaved results; D-12.8)."""
        with self._lock:
            entry = self._ephemeral.get(request_id)
        if entry is None:
            return None
        return utcnow() + timedelta(seconds=max(0.0, entry[1] - time.monotonic()))

    def is_active(self, request_id: str) -> bool:
        with self._lock:
            state = self._active.get(request_id)
        return state is not None and state.is_running

    def forget(self, request_id: str) -> None:
        """Drop a deleted request from memory and its processing copies from disk."""
        with self._lock:
            self._ephemeral.pop(request_id, None)
            self._active.pop(request_id, None)
        shutil.rmtree(self.spool_dir / request_id, ignore_errors=True)

    def latest_ephemeral(self, user_id: int) -> RequestState | None:
        """The guest's last request (D-4.11)."""
        self._purge_ephemeral()
        with self._lock:
            states = [s for s, _ in self._ephemeral.values() if s.user_id == user_id]
            states += [s for s in self._active.values() if s.user_id == user_id and not s.persist]
        return max(states, key=lambda s: s.created_at, default=None)

    @staticmethod
    def _load(request_id: str) -> RequestState | None:
        with session_scope() as db:
            req = db.get(AnalysisRequest, request_id)
            if req is None:
                return None
            owner = db.get(User, req.user_id)
            state = RequestState(
                id=req.id, user_id=req.user_id, role=Role(owner.role), model_id=req.model_id,
                provider_name=req.provider_name, model_name=req.model_name, api_type=req.api_type,
                key_ids=list(req.api_key_ids or []), own_keys=req.own_keys, persist=True, username=owner.username,
                parent_request_id=req.parent_request_id, status=req.status, created_at=as_utc(req.created_at),
                finished_at=as_utc(req.finished_at),
            )
            rows = db.scalars(select(RequestDocument).where(RequestDocument.request_id == request_id)
                              .order_by(RequestDocument.position))
            for row in rows:
                doc = DocState(
                    position=row.position, file_name=row.file_name, document_id=row.document_id, size=0,
                    status=row.status, reason=row.reason, attempts=row.attempts,
                    result_request_id=row.result_request_id, stored=row.stored,
                    temperature_applied=row.temperature_applied, input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens, total_input_tokens=row.total_input_tokens or 0,
                    total_output_tokens=row.total_output_tokens or 0, last_call_seconds=row.last_call_seconds,
                    total_call_seconds=row.total_call_seconds or 0.0, warnings=row.warnings,
                    started_at=as_utc(row.started_at),
                    finished_at=as_utc(row.finished_at), row_id=row.id,
                )
                document = db.get(Document, row.document_id)
                doc.size = document.size_bytes if document else 0
                if row.result_request_id:
                    doc.summary = results.summary_row(db, row.result_request_id, row.document_id)
                    doc.source_model = _source_model(db, req, row.result_request_id, row.document_id)
                state.docs.append(doc)
            return state

    @staticmethod
    def recent_requests(user: CurrentUser, limit: int = 10) -> list[tuple[AnalysisRequest, str]]:
        """(request, creator username), newest first; non-admins only see their own requests."""
        with session_scope() as db:
            query = (select(AnalysisRequest, User.username).join(User, User.id == AnalysisRequest.user_id)
                     .order_by(AnalysisRequest.created_at.desc()).limit(limit))
            if not user.is_admin:
                query = query.where(AnalysisRequest.user_id == user.id)
            return [(request, username) for request, username in db.execute(query)]

    # ---------------------------------------------------------------- stop (D-14.2)
    def stop(self, request_id: str, user: CurrentUser) -> None:
        """Stop a running request: waiting documents become stopped, running calls finish and are kept."""
        state = self.get(request_id, user)
        if state is None:
            raise JobError("درخواست یافت نشد.")
        if state.user_id != user.id and not user.is_admin:
            raise JobError("فقط صاحب درخواست یا مدیر سامانه می‌تواند آن را متوقف کند.")
        if not state.is_running or state.status == STOPPING:
            return
        state.status = STOPPING
        self._save_request(state)
        log.info("Request %s stopped by %s", request_id, user.username)
        if not self.scheduler.cancel(request_id):  # not queued in the engine (e.g. between steps): close it here
            for doc in state.docs:
                if doc.status in (QUEUED, PROCESSING):
                    self._set(state, doc, status=STOPPED, finished_at=utcnow(), reason=STOPPED_REASON)
            self._finish(state)

    # ---------------------------------------------------------------- retry / re-run (D-5.5, D-14.3)
    def retry(self, request_id: str, user: CurrentUser, positions: list[int] | None = None,
              key_ids: list[int] | None = None) -> int:
        """Re-queue unfinished documents (failed/interrupted/skipped/stopped) in the same request.

        `positions` limits it to those documents; `key_ids` replaces the request's keys.
        """
        state = self.get(request_id, user)
        if state is None:
            raise JobError("درخواست یافت نشد.")
        if state.is_running:
            raise JobError("این درخواست هنوز در حال اجراست.")
        if state.user_id != user.id:
            raise JobError("فقط صاحب درخواست می‌تواند آن را دوباره اجرا کند.")
        choice = next((c for c in usable_models(user.id) if c.model_id == state.model_id), None)
        allowed = {k.id for k in choice.keys} if choice else set()
        wanted = [k for k in (key_ids or state.key_ids) if k in allowed]
        if choice is None or not wanted:
            raise JobError("دسترسی شما به مدل یا کلیدهای این درخواست دیگر برقرار نیست.")
        state.key_ids = wanted
        candidates = [d for d in state.docs if d.status in RETRYABLE_STATUSES
                      and (positions is None or d.position in positions)]
        if not candidates:
            raise JobError("سند ناتمامی (ناموفق، متوقف یا ردشده) برای ادامه انتخاب نشده است.")
        with self._user_lock(user.id):
            remaining = quota_status(user.id).remaining_docs(state.own_keys, reserved=self.reserved(user.id, state.own_keys))
            requeued = []
            for doc in candidates:
                if not self._ensure_spooled(state, doc):
                    self._set(state, doc, reason="فایل این سند دیگر در دسترس نیست؛ لطفاً آن را دوباره بارگذاری کنید.")
                    continue
                if remaining is not None and len(requeued) >= remaining:
                    self._set(state, doc, status=SKIPPED_QUOTA,
                              reason="سهمیه تحلیل شما در این بازه زمانی پر است؛ بعداً از «ادامه یا اجرای مجدد» استفاده کنید.")
                    continue
                self._set(state, doc, status=QUEUED, reason=None, attempts=0, finished_at=None)
                requeued.append(doc)
            if not requeued:
                return 0
            state.status, state.finished_at = QUEUED, None
            self._save_request(state)
            with self._lock:
                self._ephemeral.pop(state.id, None)
                self._active[state.id] = state
        self._start(state, requeued)
        return len(requeued)

    def missing_files(self, state: RequestState, all_docs: bool = False) -> list[DocState]:
        """Documents whose PDF is gone: no processing copy and no kept PDF (D-12.15).

        Only documents to retry by default; `all_docs` also includes those with a result (re-run, D-14.3).
        """
        candidates = [d for d in state.docs if d.status != DUPLICATE
                      and (all_docs or d.status in RETRYABLE_STATUSES)
                      and not self._spool_file(state.id, d.document_id).exists()]
        if not candidates or not state.persist:
            return candidates
        with session_scope() as db:
            kept = set(db.scalars(select(Document.id).where(
                Document.id.in_([d.document_id for d in candidates]), Document.storage_path.is_not(None))))
        return [d for d in candidates if d.document_id not in kept]

    def provide_files(self, state: RequestState, user: CurrentUser,
                      files: list[IncomingFile]) -> tuple[list[str], list[str]]:
        """Attach re-uploaded PDFs to missing documents by SHA-256 (D-12.15). Returns (matched, rejected) names."""
        if state.user_id != user.id:
            raise JobError("فقط صاحب درخواست می‌تواند فایل‌های آن را بارگذاری کند.")
        missing = {d.document_id: d for d in self.missing_files(state, all_docs=True)}
        matched, rejected = [], []
        for item in files:
            doc = missing.pop(item.sha256, None)
            if doc is None:
                rejected.append(item.name)
                continue
            target = self._spool_file(state.id, doc.document_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.data)
            if doc.status in RETRYABLE_STATUSES:
                self._set(state, doc, reason="فایل دوباره بارگذاری شد؛ از «ادامه یا اجرای مجدد» استفاده کنید.")
            matched.append(doc.file_name)
        return matched, rejected

    def rerun(self, request_id: str, user: CurrentUser, positions: list[int], choice: ModelChoice,
              key_ids: list[int]) -> tuple[int, str | None]:
        """Re-run selected documents (D-14.3, D-14.4). Returns (resumed here, new request id or None).

        Same model: unfinished documents continue in this request, documents with a result go to a
        new linked request. Another model: everything selected goes to a new linked request.
        """
        state = self.get(request_id, user)
        if state is None:
            raise JobError("درخواست یافت نشد.")
        if state.user_id != user.id:
            raise JobError("فقط صاحب درخواست می‌تواند آن را دوباره اجرا کند.")
        if state.is_running:
            raise JobError("این درخواست هنوز در حال اجراست؛ ابتدا صبر کنید یا آن را متوقف کنید.")
        selected = [d for d in state.docs if d.position in set(positions) and d.status != DUPLICATE]
        if not selected:
            raise JobError("هیچ سندی انتخاب نشده است.")
        same_model = choice.model_id == state.model_id
        resume = [d for d in selected if same_model and d.status in RETRYABLE_STATUSES]
        fresh = [d for d in selected if d not in resume]
        files, missing = [], []
        for doc in fresh:
            data = self.pdf_bytes(state, doc)
            if data is None:
                missing.append(doc.file_name)
            else:
                files.append(IncomingFile(doc.file_name, data))
        if missing:
            raise JobError("فایل این اسناد دیگر در دسترس نیست؛ ابتدا آن‌ها را دوباره بارگذاری کنید:\n"
                           + "\n".join(f"«{name}»" for name in missing))
        resumed = self.retry(request_id, user, [d.position for d in resume], key_ids) if resume else 0
        new_id = self.submit(user, choice, key_ids, files, parent_request_id=state.id) if files else None
        log.info("Re-run of %s by %s: %d resumed, new request %s with %d document(s)", request_id, user.username,
                 resumed, new_id, len(files))
        return resumed, new_id

    def pdf_available(self, state: RequestState) -> set[str]:
        """Document ids whose PDF can still be read: processing copy or kept file (D-14.10)."""
        ids = {d.document_id for d in state.docs if self._spool_file(state.id, d.document_id).exists()}
        if state.persist:
            with session_scope() as db:
                ids |= set(db.scalars(select(Document.id).where(
                    Document.id.in_([d.document_id for d in state.docs]), Document.storage_path.is_not(None))))
        return ids

    def pdf_bytes(self, state: RequestState, doc: DocState) -> bytes | None:
        source = self._spool_file(state.id, doc.document_id)
        if source.exists():
            return source.read_bytes()
        if not state.persist:
            return None
        with session_scope() as db:
            document = db.get(Document, doc.document_id)
            path = document.storage_path if document else None
        return get_storage().get(path) if path else None

    def _ensure_spooled(self, state: RequestState, doc: DocState) -> bool:
        target = self._spool_file(state.id, doc.document_id)
        if target.exists():
            return True
        if not state.persist:
            return False
        with session_scope() as db:
            document = db.get(Document, doc.document_id)
            path = document.storage_path if document else None
        data = get_storage().get(path) if path else None
        if data is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return True

    # ---------------------------------------------------------------- startup recovery (D-10.2)
    def recover_interrupted(self) -> int:
        """Mark work left unfinished by a previous server process as interrupted."""
        with session_scope() as db:
            count = db.execute(
                update(RequestDocument).where(RequestDocument.status.in_([QUEUED, PROCESSING])).values(
                    status=INTERRUPTED, finished_at=utcnow(),
                    reason="پردازش با راه‌اندازی مجدد سرور متوقف شد؛ از «ادامه یا اجرای مجدد» استفاده کنید.")
            ).rowcount
            db.execute(update(AnalysisRequest).where(AnalysisRequest.status.in_(RUNNING_STATUSES))
                       .values(status=INTERRUPTED, finished_at=utcnow()))
        cutoff = time.time() - SPOOL_KEEP_DAYS * 86400
        if self.spool_dir.exists():
            for folder in self.spool_dir.iterdir():
                if folder.is_dir() and folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
        return count


_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_job_manager() -> JobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
            _manager.recover_interrupted()
        return _manager

