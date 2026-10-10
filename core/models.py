"""SQLAlchemy models (data model: docs/PLAN.md). Schema changes go through Alembic (migrations/)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from core.sheets import SHEETS


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"
    GUEST = "guest"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    # Incremented to invalidate all login sessions of the user (password change, reset, deactivation).
    session_version: Mapped[int] = mapped_column(Integer, default=1)

    # Quotas (NULL = not applicable / unlimited, depending on role)
    docs_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    docs_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    db_quota_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_quota_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    guest_doc_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def role_enum(self) -> Role:
        return Role(self.role)

    def is_expired(self, now: datetime | None = None) -> bool:
        expires = as_utc(self.expires_at)
        return expires is not None and expires <= (now or utcnow())

    def is_locked(self, now: datetime | None = None) -> bool:
        locked = as_utc(self.locked_until)
        return locked is not None and locked > (now or utcnow())


class LoginTicket(Base):
    """One-time hand-off from the login form to the cookie route (D-11.2).

    Kept in the database, not in process memory, so the ASGI route and the Streamlit script
    always see the same tickets. Only the SHA-256 of the ticket is stored; rows live ~60 s.
    """

    __tablename__ = "login_tickets"

    ticket_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# --- Providers, models, API keys, access (D-6) ------------------------------------------


class ApiType(StrEnum):
    GEMINI = "gemini"
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    MOCK = "mock"  # development only (D-10.7)


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NULL = defined by the admin (shared through access grants); otherwise a user's private provider
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    api_type: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LlmModel(Base):
    __tablename__ = "llm_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))  # model id sent to the API
    display_name: Mapped[str] = mapped_column(String(128))
    supports_pdf: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_temperature: Mapped[bool] = mapped_column(Boolean, default=True)  # D-2.4
    max_file_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Default limits for this model's keys (D-11.9); NULL = unlimited
    rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_s: Mapped[int] = mapped_column(Integer, default=300)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(64))
    key_encrypted: Mapped[str] = mapped_column(Text)
    key_last4: Mapped[str] = mapped_column(String(8))
    # Per-key overrides of the model limits (D-11.9); NULL = use the model's value
    rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    concurrency: Mapped[int] = mapped_column(Integer, default=1)  # simultaneous calls per key x model
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessGrant(Base):
    """Access to an admin-defined provider for a role (default) or a user (override), D-6.3.

    model_ids / api_key_ids: JSON lists of ids, NULL = all models / all keys of the provider.
    effect: "allow" (roles and users) or "deny" (user overrides only).
    """

    __tablename__ = "access_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    model_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    api_key_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    effect: Mapped[str] = mapped_column(String(8), default="allow")


class AppSetting(Base):
    """Admin-editable server settings (e.g. the server-wide cap on simultaneous API calls, D-11.11)."""

    __tablename__ = "app_settings"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | int | str | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KeyUsageDaily(Base):
    """Per key x model daily counters (RPD limits, usage stats). `day` is the UTC date (YYYY-MM-DD)."""

    __tablename__ = "key_usage_daily"

    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("llm_models.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)


# --- Requests, documents, results (D-5, D-10) ---------------------------------------------


class AnalysisRequest(Base):
    __tablename__ = "analysis_requests"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)  # e.g. R-20261008-K3Q9
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id", ondelete="SET NULL"), nullable=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("llm_models.id", ondelete="SET NULL"), nullable=True)
    # Snapshots, so history stays readable after providers/models are edited or deleted
    provider_name: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(128))
    api_type: Mapped[str] = mapped_column(String(32))
    api_key_ids: Mapped[list] = mapped_column(JSON, default=list)
    own_keys: Mapped[bool] = mapped_column(Boolean, default=False)  # private provider: doc quotas waived (D-6.4)
    # A re-run of documents that already had a result (D-14.3): the request it came from
    parent_request_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Document(Base):
    """A distinct PDF, identified by the SHA-256 of its bytes (D-5.2)."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str | None] = mapped_column(String(255), nullable=True)  # NULL = not kept
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserDocument(Base):
    """A user's kept copy of a PDF (storage quota accounting, D-10.10); physically stored once."""

    __tablename__ = "user_documents"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    file_name: Mapped[str] = mapped_column(Text)  # as uploaded; ZIP paths can be long (D-11.17)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RequestDocument(Base):
    """One uploaded file inside a request, with its processing status (D-5.4)."""

    __tablename__ = "request_documents"
    __table_args__ = (Index("ix_reqdoc_request_document", "request_id", "document_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("analysis_requests.id", ondelete="CASCADE"))
    document_id: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(Text)  # as uploaded; ZIP paths can be long (D-11.17)
    status: Mapped[str] = mapped_column(String(24), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # Persian, user-facing
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # The stored result this row uses: its own (== request_id) or a reused one (cache, D-5.6)
    result_request_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    stored: Mapped[bool] = mapped_column(Boolean, default=False)
    temperature_applied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)  # last attempt that got an answer
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)  # all attempts with an answer (D-14.6)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    last_call_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)  # model time (D-14.5)
    total_call_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)  # D-11.18, e.g. extra text after the JSON
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalysisResult(Base):
    """A stored LLM result, unique by the request that produced it and the document (D-5.2)."""

    __tablename__ = "analysis_results"

    request_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_name: Mapped[str] = mapped_column(String(64))
    model_name: Mapped[str] = mapped_column(String(128))
    api_type: Mapped[str] = mapped_column(String(32))
    temperature_applied: Mapped[bool] = mapped_column(Boolean)
    raw_json_gz: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)  # gzip JSON + sheet text bytes (D-10.10)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)  # D-11.18; NULL = clean answer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawOutput(Base):
    """The model's answer kept verbatim when it was not a clean JSON (D-11.18).

    kind "accepted": a JSON followed by extra text; the result was used and carries a warning.
    kind "rejected": the answer could not be used (invalid JSON, wrong structure, truncated, refusal);
    one row per attempt. Counted in the user's results quota.
    """

    __tablename__ = "raw_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(24), index=True)
    document_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # accepted | rejected
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str] = mapped_column(String(128))
    raw_text_gz: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RequestShare(Base):
    """A request shared read-only with a viewer (D-4.3, D-12.6)."""

    __tablename__ = "request_shares"

    request_id: Mapped[str] = mapped_column(ForeignKey("analysis_requests.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    shared_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CompanyMerge(Base):
    """A user's manual company merge: `member_key` is shown as `main_key` (D-13.5). Keys: D-13.4."""

    __tablename__ = "company_merges"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    member_key: Mapped[str] = mapped_column(Text, primary_key=True)
    main_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageEvent(Base):
    """One successful new API analysis (quota accounting, D-4.4). Tiny; also written for guests."""

    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_user_at", "user_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    request_id: Mapped[str] = mapped_column(String(24))
    document_id: Mapped[str] = mapped_column(String(64))
    own_key: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Sheet tables (D-8.2), generated from core.sheets ---------------------------------------


def _sheet_table(spec):
    from sqlalchemy import Column, Table

    columns = [
        Column("request_id", String(24), primary_key=True),
        Column("document_id", String(64), primary_key=True),
        Column("row_no", Integer, primary_key=True),
    ]
    columns += [Column(name, Integer if name == "year" else Text, nullable=True) for name, _ in spec.columns]
    columns.append(Column("extra", JSON, nullable=True))  # unexpected keys from the model, kept verbatim
    return Table(spec.table, Base.metadata, *columns)


SHEET_TABLES = {spec.key: _sheet_table(spec) for spec in SHEETS}
