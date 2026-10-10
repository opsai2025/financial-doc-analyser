"""Admin-editable server settings stored in `app_settings` (D-11.11)."""

from __future__ import annotations

import threading
import time

from core.config import DEFAULT_MAX_PARALLEL_CALLS, MAX_PARALLEL_CALLS_RANGE
from core.db import session_scope
from core.fa import fa_digits
from core.models import AppSetting

MAX_PARALLEL_CALLS = "max_parallel_calls"

_CACHE_SECONDS = 5.0
_cache: dict[str, tuple[int, float]] = {}
_cache_lock = threading.Lock()


class SettingError(ValueError):
    """Invalid value, with a Persian message."""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def max_parallel_calls() -> int:
    """Server-wide cap on simultaneous API calls. Cached for a few seconds (read on every scheduling pass)."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(MAX_PARALLEL_CALLS)
        if cached and cached[1] > now:
            return cached[0]
    with session_scope() as db:
        row = db.get(AppSetting, MAX_PARALLEL_CALLS)
        value = row.value if row is not None and isinstance(row.value, int) else DEFAULT_MAX_PARALLEL_CALLS
    with _cache_lock:
        _cache[MAX_PARALLEL_CALLS] = (value, now + _CACHE_SECONDS)
    return value


def set_max_parallel_calls(value: int) -> None:
    low, high = MAX_PARALLEL_CALLS_RANGE
    if not low <= value <= high:
        raise SettingError(f"حداکثر درخواست هم‌زمان سرور باید بین {fa_digits(low)} تا {fa_digits(high)} باشد.")
    with session_scope() as db:
        row = db.get(AppSetting, MAX_PARALLEL_CALLS) or AppSetting(name=MAX_PARALLEL_CALLS)
        row.value = int(value)
        db.add(row)
    with _cache_lock:
        _cache.pop(MAX_PARALLEL_CALLS, None)
