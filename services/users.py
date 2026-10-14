"""User management (admin): create, edit, reset password, unlock (D-4.*)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from core.db import session_scope
from core.models import Role, User, utcnow
from core.security import USERNAME_HINT, USERNAME_RE, generate_temp_password, hash_password, password_policy_errors
from core.text_normalize import normalize_text
from services import app_settings

ROLE_LABELS: dict[Role, str] = {
    Role.ADMIN: "مدیر سامانه",
    Role.ANALYST: "تحلیلگر",
    Role.VIEWER: "مشاهده‌گر",
    Role.GUEST: "مهمان",
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.ADMIN: "دسترسی کامل؛ مدیریت کاربران و تنظیمات؛ بدون محدودیت سهمیه.",
    Role.ANALYST: "بارگذاری و تحلیل اسناد؛ مشاهده تحلیل‌های خود؛ دارای سهمیه ساعتی، روزانه و فضای ذخیره‌سازی.",
    Role.VIEWER: "فقط مشاهده تحلیل‌هایی که با او به اشتراک گذاشته شده است؛ امکان تحلیل ندارد.",
    Role.GUEST: "تحلیل محدود (سقف کل اسناد و تاریخ انقضا)؛ هیچ داده‌ای ذخیره نمی‌شود و فقط خروجی اکسل دارد.",
}


class UserError(ValueError):
    """Validation error with a Persian, user-facing message."""


@dataclass
class UserForm:
    """Editable user fields. Quota fields not relevant to the role are ignored (stored as NULL)."""

    display_name: str
    role: Role
    is_active: bool = True
    docs_per_hour: int | None = None
    docs_per_day: int | None = None
    db_quota_mb: int | None = None
    storage_quota_mb: int | None = None
    guest_doc_cap: int | None = None
    guest_valid_days: int | None = None  # used to (re)compute expires_at for guests
    errors: list[str] = field(default_factory=list)


def default_form(role: Role) -> UserForm:
    """A new account pre-filled with the admin's current defaults (D-16.3, D-16.4)."""
    form = UserForm(display_name="", role=role)
    if role is Role.ANALYST:
        d = app_settings.get_many(("new_docs_per_hour", "new_docs_per_day", "new_db_quota_mb", "new_storage_quota_mb"))
        form.docs_per_hour, form.docs_per_day = d["new_docs_per_hour"], d["new_docs_per_day"]
        form.db_quota_mb, form.storage_quota_mb = d["new_db_quota_mb"], d["new_storage_quota_mb"]
    elif role is Role.GUEST:
        d = app_settings.get_many(("new_guest_doc_cap", "new_guest_valid_days"))
        form.guest_doc_cap, form.guest_valid_days = d["new_guest_doc_cap"], d["new_guest_valid_days"]
    return form


def _validate_form(form: UserForm) -> list[str]:
    errors = []
    name = normalize_text(form.display_name)
    if not isinstance(name, str) or not 2 <= len(name) <= 128:
        errors.append("نام نمایشی باید بین ۲ تا ۱۲۸ نویسه باشد.")
    if form.role is Role.ANALYST:
        for value, label in (
            (form.docs_per_hour, "سقف اسناد در ساعت"),
            (form.docs_per_day, "سقف اسناد در روز"),
            (form.db_quota_mb, "سهمیه پایگاه داده"),
            (form.storage_quota_mb, "سهمیه فضای فایل"),
        ):
            if value is None or value < 0:
                errors.append(f"مقدار «{label}» باید عدد صفر یا مثبت باشد.")
        if (form.docs_per_hour or 0) > (form.docs_per_day or 0):
            errors.append("سقف ساعتی نمی‌تواند از سقف روزانه بیشتر باشد.")
    if form.role is Role.GUEST:
        if form.guest_doc_cap is None or form.guest_doc_cap < 1:
            errors.append("سقف کل اسناد مهمان باید حداقل ۱ باشد.")
        if form.guest_valid_days is not None and form.guest_valid_days < 1:
            errors.append("مدت اعتبار حساب مهمان باید حداقل ۱ روز باشد.")
    return errors


def _apply_form(user: User, form: UserForm, now: datetime) -> None:
    user.display_name = normalize_text(form.display_name)  # type: ignore[assignment]
    user.role = form.role
    user.is_active = form.is_active
    is_analyst = form.role is Role.ANALYST
    is_guest = form.role is Role.GUEST
    user.docs_per_hour = form.docs_per_hour if is_analyst else None
    user.docs_per_day = form.docs_per_day if is_analyst else None
    user.db_quota_mb = form.db_quota_mb if is_analyst else None
    user.storage_quota_mb = form.storage_quota_mb if is_analyst else None
    user.guest_doc_cap = form.guest_doc_cap if is_guest else None
    if not is_guest:
        user.expires_at = None
    elif form.guest_valid_days is not None:
        user.expires_at = now + timedelta(days=form.guest_valid_days)


def list_users() -> list[User]:
    with session_scope() as db:
        return list(db.scalars(select(User).order_by(User.created_at)))


def get_user(user_id: int) -> User | None:
    with session_scope() as db:
        return db.get(User, user_id)


def create_user(actor_id: int, username: str, form: UserForm, password: str | None = None) -> tuple[User, str]:
    """Create a user. Returns (user, temporary password). Raises UserError."""
    username = username.strip().lower()
    errors = [] if USERNAME_RE.match(username) else [USERNAME_HINT]
    errors += _validate_form(form)
    if form.role is Role.GUEST and form.guest_valid_days is None:
        errors.append("مدت اعتبار حساب مهمان را مشخص کنید.")
    temp_password = password or generate_temp_password()
    errors += password_policy_errors(temp_password)
    if errors:
        raise UserError("\n".join(errors))
    now = utcnow()
    with session_scope() as db:
        if db.scalar(select(User.id).where(User.username == username)) is not None:
            raise UserError("این نام کاربری قبلاً ثبت شده است.")
        user = User(
            username=username,
            password_hash=hash_password(temp_password),
            must_change_password=True,
            created_by_id=actor_id,
        )
        _apply_form(user, form, now)
        db.add(user)
        db.flush()
        return user, temp_password


def _active_admin_count(db, exclude_id: int | None = None) -> int:
    query = select(func.count()).select_from(User).where(User.role == Role.ADMIN, User.is_active.is_(True))
    if exclude_id is not None:
        query = query.where(User.id != exclude_id)
    return db.scalar(query) or 0


def update_user(actor_id: int, user_id: int, form: UserForm) -> User:
    errors = _validate_form(form)
    if errors:
        raise UserError("\n".join(errors))
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            raise UserError("کاربر یافت نشد.")
        losing_admin = user.role == Role.ADMIN and (form.role is not Role.ADMIN or not form.is_active)
        if losing_admin and user_id == actor_id:
            raise UserError("شما نمی‌توانید نقش مدیریتی خود را حذف یا حساب خود را غیرفعال کنید.")
        if losing_admin and _active_admin_count(db, exclude_id=user_id) == 0:
            raise UserError("حداقل یک مدیر فعال باید در سامانه باقی بماند.")
        was_active = user.is_active
        role_changed = user.role != form.role
        if form.role is Role.GUEST and user.expires_at is None and form.guest_valid_days is None:
            raise UserError("مدت اعتبار حساب مهمان را مشخص کنید.")
        _apply_form(user, form, utcnow())
        if (was_active and not user.is_active) or role_changed:
            user.session_version += 1  # sign the user out everywhere
        return user


def reset_password(actor_id: int, user_id: int) -> str:
    """Set a new temporary password, force change at next login, revoke sessions."""
    temp_password = generate_temp_password()
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            raise UserError("کاربر یافت نشد.")
        user.password_hash = hash_password(temp_password)
        user.must_change_password = True
        user.failed_logins = 0
        user.locked_until = None
        user.session_version += 1
    return temp_password


def unlock_user(user_id: int) -> None:
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is not None:
            user.failed_logins = 0
            user.locked_until = None


def user_status(user: User, now: datetime | None = None) -> str:
    now = now or utcnow()
    if not user.is_active:
        return "غیرفعال"
    if user.is_expired(now):
        return "منقضی"
    if user.is_locked(now):
        return "قفل موقت"
    if user.must_change_password:
        return "در انتظار تغییر رمز"
    return "فعال"


def form_from_user(user: User) -> UserForm:
    """Form for editing; guest_valid_days stays None so the expiry is kept unless the admin extends it."""
    return UserForm(
        display_name=user.display_name,
        role=user.role_enum,
        is_active=user.is_active,
        docs_per_hour=user.docs_per_hour,
        docs_per_day=user.docs_per_day,
        db_quota_mb=user.db_quota_mb,
        storage_quota_mb=user.storage_quota_mb,
        guest_doc_cap=user.guest_doc_cap,
    )
