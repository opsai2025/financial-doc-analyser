"""Admin-editable server settings stored in `app_settings` (D-11.11, D-16.3–D-16.6).

One row per setting; a missing row means the default from `core/config.py`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from sqlalchemy import select

from core import config
from core.db import session_scope
from core.fa import fa_digits
from core.models import AppSetting


@dataclass(frozen=True)
class Setting:
    name: str
    label: str  # Persian, used in validation messages and the settings form
    default: int
    low: int
    high: int


def _settings(*items: Setting) -> dict[str, Setting]:
    return {s.name: s for s in items}


SETTINGS = _settings(
    Setting("max_parallel_calls", "حداکثر درخواست هم‌زمان سرور", config.DEFAULT_MAX_PARALLEL_CALLS,
            *config.MAX_PARALLEL_CALLS_RANGE),
    # Pre-filled quotas for new accounts (D-16.3, D-16.4: existing users keep their own values)
    Setting("new_docs_per_hour", "سقف اسناد در ساعت", config.DEFAULT_ANALYST_DOCS_PER_HOUR, 0, 10_000),
    Setting("new_docs_per_day", "سقف اسناد در روز", config.DEFAULT_ANALYST_DOCS_PER_DAY, 0, 100_000),
    Setting("new_db_quota_mb", "سهمیه نتایج در پایگاه داده (مگابایت)", config.DEFAULT_ANALYST_DB_QUOTA_MB, 0, 500),
    Setting("new_storage_quota_mb", "سهمیه فایل‌های PDF (مگابایت)", config.DEFAULT_ANALYST_STORAGE_QUOTA_MB, 0, 1000),
    Setting("new_guest_doc_cap", "سقف کل اسناد مهمان", config.DEFAULT_GUEST_DOC_CAP, 1, 1000),
    Setting("new_guest_valid_days", "مدت اعتبار حساب مهمان (روز)", config.DEFAULT_GUEST_VALID_DAYS, 1, 365),
    # Upload limits, for everyone at once. The ZIP limit can't exceed server.maxUploadSize (300 MB).
    Setting("max_pdf_mb", "حداکثر حجم هر فایل PDF (مگابایت)", config.MAX_PDF_MB, 1, 50),
    Setting("max_files_per_batch", "حداکثر تعداد فایل در یک درخواست", config.MAX_FILES_PER_BATCH, 1, 200),
    Setting("max_zip_mb", "حداکثر حجم محتوای ZIP (مگابایت)", config.MAX_ZIP_UNCOMPRESSED_MB, 1, 300),
    # Plan limits of the hosting database and file storage (D-16.5)
    Setting("db_limit_mb", "سقف پایگاه داده در طرح Supabase (مگابایت)", config.SUPABASE_DB_LIMIT_MB, 1, 1_000_000),
    Setting("storage_limit_mb", "سقف فضای فایل در طرح Supabase (مگابایت)", config.SUPABASE_STORAGE_LIMIT_MB,
            1, 1_000_000),
)
MAX_PARALLEL_CALLS = "max_parallel_calls"

_CACHE_SECONDS = 5.0
_cache: dict[str, tuple[int, float]] = {}
_cache_lock = threading.Lock()


class SettingError(ValueError):
    """Invalid value, with a Persian message."""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get(name: str) -> int:
    """Current value; cached for a few seconds (some settings are read on every scheduling pass)."""
    setting = SETTINGS[name]
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(name)
        if cached and cached[1] > now:
            return cached[0]
    with session_scope() as db:
        row = db.get(AppSetting, name)
        value = row.value if row is not None and isinstance(row.value, int) else setting.default
    with _cache_lock:
        _cache[name] = (value, now + _CACHE_SECONDS)
    return value


def get_many(names) -> dict[str, int]:
    names = list(names)
    with session_scope() as db:
        stored = {r.name: r.value for r in db.scalars(select(AppSetting).where(AppSetting.name.in_(names)))}
    return {n: stored[n] if isinstance(stored.get(n), int) else SETTINGS[n].default for n in names}


def _check(values: dict[str, int]) -> list[str]:
    errors = []
    for name, value in values.items():
        s = SETTINGS[name]
        if not s.low <= value <= s.high:
            errors.append(f"«{s.label}» باید بین {fa_digits(s.low)} تا {fa_digits(s.high)} باشد.")
    merged = {**get_many(SETTINGS), **values}
    if merged["new_docs_per_hour"] > merged["new_docs_per_day"]:
        errors.append("سقف ساعتی نمی‌تواند از سقف روزانه بیشتر باشد.")
    if merged["max_zip_mb"] < merged["max_pdf_mb"]:
        errors.append("حجم محتوای ZIP نمی‌تواند از حجم یک فایل PDF کمتر باشد.")
    return errors


def set_many(values: dict[str, int]) -> None:
    """Validate and save several settings at once; nothing is saved when one is invalid."""
    values = {name: int(value) for name, value in values.items()}
    errors = _check(values)
    if errors:
        raise SettingError("\n".join(errors))
    with session_scope() as db:
        for name, value in values.items():
            row = db.get(AppSetting, name) or AppSetting(name=name)
            row.value = value
            db.add(row)
    with _cache_lock:
        for name in values:
            _cache.pop(name, None)


def max_parallel_calls() -> int:
    """Server-wide cap on simultaneous API calls (D-11.11)."""
    return get(MAX_PARALLEL_CALLS)


def set_max_parallel_calls(value: int) -> None:
    low, high = config.MAX_PARALLEL_CALLS_RANGE
    if not low <= value <= high:
        raise SettingError(f"حداکثر درخواست هم‌زمان سرور باید بین {fa_digits(low)} تا {fa_digits(high)} باشد.")
    set_many({MAX_PARALLEL_CALLS: value})


@dataclass(frozen=True)
class UploadLimits:
    max_pdf_mb: int = config.MAX_PDF_MB
    max_files: int = config.MAX_FILES_PER_BATCH
    max_zip_mb: int = config.MAX_ZIP_UNCOMPRESSED_MB


def upload_limits() -> UploadLimits:
    return UploadLimits(get("max_pdf_mb"), get("max_files_per_batch"), get("max_zip_mb"))
