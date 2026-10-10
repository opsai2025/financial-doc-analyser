"""Authentication: login with lockout, session tokens, one-time login tickets, password change."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, func, select

from core.config import LOCKOUT_MINUTES, MAX_FAILED_LOGINS, get_settings
from core.db import session_scope
from core.models import LoginTicket, Role, User, as_utc, utcnow
from core.security import DUMMY_HASH, hash_password, password_policy_errors, read_session, sign_session, verify_password

SESSION_COOKIE = "fda_session"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentUser:
    """Immutable snapshot of the logged-in user kept in st.session_state."""

    id: int
    username: str
    display_name: str
    role: Role
    must_change_password: bool
    session_version: int
    docs_per_hour: int | None
    docs_per_day: int | None
    db_quota_mb: int | None
    storage_quota_mb: int | None
    guest_doc_cap: int | None
    expires_at: datetime | None
    last_login_at: datetime | None
    loaded_at: float

    @classmethod
    def from_model(cls, user: User) -> CurrentUser:
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role_enum,
            must_change_password=user.must_change_password,
            session_version=user.session_version,
            docs_per_hour=user.docs_per_hour,
            docs_per_day=user.docs_per_day,
            db_quota_mb=user.db_quota_mb,
            storage_quota_mb=user.storage_quota_mb,
            guest_doc_cap=user.guest_doc_cap,
            expires_at=as_utc(user.expires_at),
            last_login_at=as_utc(user.last_login_at),
            loaded_at=time.monotonic(),
        )

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN


class LoginError(StrEnum):
    INVALID = "invalid"
    LOCKED = "locked"
    INACTIVE = "inactive"
    EXPIRED = "expired"


@dataclass(frozen=True)
class LoginResult:
    user: CurrentUser | None = None
    error: LoginError | None = None
    locked_until: datetime | None = None


def authenticate(username: str, password: str) -> LoginResult:
    result = _authenticate(username.strip().lower(), password)
    if result.user is not None:
        log.info("Login succeeded: user=%s role=%s", result.user.username, result.user.role)
    else:
        log.info("Login refused: user=%s reason=%s", username.strip().lower()[:32], result.error)
    return result


def _authenticate(username: str, password: str) -> LoginResult:
    now = utcnow()
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            verify_password(password, DUMMY_HASH)  # keep timing similar
            return LoginResult(error=LoginError.INVALID)
        if user.is_locked(now):
            return LoginResult(error=LoginError.LOCKED, locked_until=as_utc(user.locked_until))
        if not verify_password(password, user.password_hash):
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.failed_logins = 0
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                return LoginResult(error=LoginError.LOCKED, locked_until=user.locked_until)
            return LoginResult(error=LoginError.INVALID)
        # Password is correct from here on: it is safe to reveal account state.
        user.failed_logins = 0
        user.locked_until = None
        if not user.is_active:
            return LoginResult(error=LoginError.INACTIVE)
        if user.is_expired(now):
            return LoginResult(error=LoginError.EXPIRED)
        user.last_login_at = now
        return LoginResult(user=CurrentUser.from_model(user))


def load_active_user(user_id: int, session_version: int | None = None) -> CurrentUser | None:
    """Reload a user; None if missing, inactive, expired or the session was revoked."""
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active or user.is_expired():
            return None
        if session_version is not None and user.session_version != session_version:
            return None
        return CurrentUser.from_model(user)


def issue_session_token(user: CurrentUser) -> str:
    return sign_session(get_settings().app_secret_key, user.id, user.session_version)


def user_from_session_token(token: str | None) -> CurrentUser | None:
    if not token:
        return None
    settings = get_settings()
    parsed = read_session(settings.app_secret_key, token, settings.session_hours * 3600)
    if parsed is None:
        return None
    return load_active_user(*parsed)


def change_password(user_id: int, current_password: str, new_password: str) -> tuple[CurrentUser | None, list[str]]:
    """Change own password. Returns (updated user, errors). Revokes all other sessions."""
    errors = password_policy_errors(new_password)
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            return None, ["حساب کاربری معتبر نیست."]
        if not verify_password(current_password, user.password_hash):
            errors.insert(0, "رمز عبور فعلی نادرست است.")
        if verify_password(new_password, user.password_hash):
            errors.append("رمز عبور جدید باید با رمز فعلی متفاوت باشد.")
        if errors:
            return None, errors
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        user.password_changed_at = utcnow()
        user.session_version += 1
        db.flush()
        return CurrentUser.from_model(user), []


def ensure_bootstrap_admin() -> str | None:
    """Create the bootstrap admin from .env if no active admin exists (D-4.2).

    Returns a Persian error message if an admin is needed but cannot be created.
    """
    settings = get_settings()
    with session_scope() as db:
        active_admins = db.scalar(
            select(func.count()).select_from(User).where(User.role == Role.ADMIN, User.is_active.is_(True))
        )
        if active_admins:
            return None
        if not settings.admin_initial_password:
            return "هیچ مدیر فعالی وجود ندارد. مقدار ADMIN_INITIAL_PASSWORD را در فایل ‎.env‎ تنظیم کنید."
        if password_policy_errors(settings.admin_initial_password):
            return "مقدار ADMIN_INITIAL_PASSWORD با سیاست رمز عبور (حداقل ۸ نویسه، شامل حرف و رقم) سازگار نیست."
        existing = db.scalar(select(User).where(User.username == settings.admin_username))
        if existing is not None:
            # Reactivate/promote the configured account instead of creating a duplicate.
            existing.role = Role.ADMIN
            existing.is_active = True
            existing.password_hash = hash_password(settings.admin_initial_password)
            existing.must_change_password = True
            existing.session_version += 1
            existing.expires_at = None
            return None
        db.add(
            User(
                username=settings.admin_username,
                display_name="مدیر سامانه",
                password_hash=hash_password(settings.admin_initial_password),
                role=Role.ADMIN,
                must_change_password=True,
            )
        )
        return None


# --- One-time login tickets (D-11.2) ------------------------------------------
# The Streamlit script cannot set HTTP cookies. After a successful login it creates a
# short-lived single-use ticket and redirects the browser to /auth/session, an ASGI route
# (see app.py) that exchanges the ticket for an HttpOnly session cookie.
# Tickets live in the database: the route and the script run in different contexts (and,
# behind a load balancer, possibly in different processes), so process memory is not shared
# state they can rely on.

TICKET_TTL_SECONDS = 60


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def create_login_ticket(user: CurrentUser) -> str:
    ticket = secrets.token_urlsafe(32)
    now = utcnow()
    with session_scope() as db:
        db.execute(delete(LoginTicket).where(LoginTicket.expires_at < now))
        db.add(LoginTicket(ticket_hash=_ticket_hash(ticket), user_id=user.id, session_version=user.session_version,
                           expires_at=now + timedelta(seconds=TICKET_TTL_SECONDS)))
    return ticket


class TicketError(StrEnum):
    MISSING = "missing"  # no ticket in the URL
    UNKNOWN = "unknown"  # never issued, or already used
    EXPIRED = "expired"
    REVOKED = "revoked"  # user deactivated/expired or session revoked in the meantime


def redeem_login_ticket(ticket: str) -> tuple[str | None, TicketError | None]:
    """Consume a ticket (single use). Returns (session token, None) or (None, reason)."""
    if not ticket:
        return None, TicketError.MISSING
    key = _ticket_hash(ticket)
    with session_scope() as db:
        row = db.get(LoginTicket, key)
        if row is None:
            return None, TicketError.UNKNOWN
        user_id, version, expires_at = row.user_id, row.session_version, as_utc(row.expires_at)
        # The DELETE is the single-use gate: of two concurrent redemptions only one removes the row.
        if db.execute(delete(LoginTicket).where(LoginTicket.ticket_hash == key)).rowcount != 1:
            return None, TicketError.UNKNOWN
    if expires_at < utcnow():
        return None, TicketError.EXPIRED
    user = load_active_user(user_id, version)
    if user is None:
        return None, TicketError.REVOKED
    return issue_session_token(user), None
