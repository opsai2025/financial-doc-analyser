"""Dispatch scheduler: who may call which key, when (D-11.9 .. D-11.14; ENGINE.md).

* One scheduler per process decides every dispatch under one lock (`_cond`).
* A worker thread only runs a real API call. Waiting for a key, for the minute window, for a
  cooldown or for a retry is a queue state, never a sleep inside a thread.
* Capacity is reserved before dispatch and released when the call ends: a slot of the key x model
  (its concurrency), a start in the 60 s RPM window, a reservation against RPD, a server slot.
* Limits are counted per key x model and shared by all users; edits apply within seconds.
* Turns rotate by user, then by request in submission order (D-11.12).

The scheduler knows nothing about storage or the UI: the JobManager plugs in a `Handler`.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from core.config import MAX_PARALLEL_CALLS_RANGE, RETRY_BACKOFF_S
from core.db import session_scope
from core.fa import TEHRAN_TZ, fa_digits
from core.models import KeyUsageDaily
from services.app_settings import max_parallel_calls
from services.llm import ErrorKind, LlmError
from services.llm.base import AnalysisOutput, short_error
from services.providers import KeyLimits, KeyRuntime, live_limits

log = logging.getLogger(__name__)

WINDOW_S = 60.0  # RPM window
DEFAULT_COOLDOWN_S = 60.0  # after a 429 without Retry-After
REFRESH_S = 10.0  # live limits, daily counters and the server cap are re-read this often


def utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def clock_time_in(seconds: float) -> str:
    """Tehran wall-clock time `seconds` from now, Persian digits (stable text for wait reasons)."""
    return fa_digits((datetime.now(TEHRAN_TZ) + timedelta(seconds=max(0.0, seconds))).strftime("%H:%M:%S"))


# --------------------------------------------------------------------------- daily counters (DB)
def used_today(pairs: set[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """Calls the provider answered today, per (key_id, model_id)."""
    if not pairs:
        return {}
    day = utc_day()
    with session_scope() as db:
        rows = db.query(KeyUsageDaily).filter(KeyUsageDaily.day == day,
                                              KeyUsageDaily.api_key_id.in_({k for k, _ in pairs})).all()
        found = {(r.api_key_id, r.model_id): r.requests for r in rows}
    return {pair: found.get(pair, 0) for pair in pairs}


def record_usage(key_id: int, model_id: int, input_tokens: int | None, output_tokens: int | None) -> None:
    with session_scope() as db:
        day = utc_day()
        row = db.get(KeyUsageDaily, (key_id, model_id, day))
        if row is None:
            row = KeyUsageDaily(api_key_id=key_id, model_id=model_id, day=day, requests=0, input_tokens=0,
                                output_tokens=0)
            db.add(row)
        row.requests += 1
        row.input_tokens += input_tokens or 0
        row.output_tokens += output_tokens or 0


# --------------------------------------------------------------------------- capacity of one key x model
class Block(StrEnum):
    NONE = "none"  # a call may start now
    INACTIVE = "inactive"  # key or model deactivated/deleted
    DAILY = "daily"  # RPD used up for today
    COOLDOWN = "cooldown"  # provider answered 429 recently
    RPM = "rpm"  # minute window full
    BUSY = "busy"  # all concurrency slots in use (or the rest of RPD is reserved by running calls)


@dataclass
class Slot:
    """Capacity of one API key for one model, shared by every user and request."""

    key_id: int
    model_id: int
    key_label: str
    model_label: str
    limits: KeyLimits
    running: int = 0
    starts: deque = field(default_factory=deque)  # start times inside the RPM window
    cooldown_until: float = 0.0
    day: str = ""
    used: int = 0  # calls the provider answered on `day`

    def state(self, now: float) -> tuple[Block, float]:
        """Why no call may start now, and seconds until one might (inf = when a call ends)."""
        if not self.limits.active:
            return Block.INACTIVE, math.inf
        today = utc_day()
        if self.day != today:
            self.day, self.used = today, 0
        rpd = self.limits.rpd
        if rpd is not None and self.used >= rpd:
            return Block.DAILY, math.inf
        if now < self.cooldown_until:
            return Block.COOLDOWN, self.cooldown_until - now
        while self.starts and now - self.starts[0] >= WINDOW_S:
            self.starts.popleft()
        rpm = self.limits.rpm
        if rpm is not None and len(self.starts) >= rpm:
            return Block.RPM, WINDOW_S - (now - self.starts[len(self.starts) - rpm])
        if self.running >= self.limits.concurrency or (rpd is not None and self.used + self.running >= rpd):
            return Block.BUSY, math.inf
        return Block.NONE, 0.0


# --------------------------------------------------------------------------- queued work
@dataclass
class Task:
    """One document of a request: an ordered series of attempts (D-5.4: isolated from the others)."""

    run: Run
    doc: object  # services.jobs.DocState
    position: int
    attempts: int = 0
    not_before: float = 0.0  # retry delay
    last_key: int | None = None
    last_error: LlmError | None = None
    reason: str | None = None  # last waiting reason reported


@dataclass
class Run:
    """A request's documents inside the scheduler."""

    request_id: str
    user_id: int
    username: str
    model_id: int
    model_label: str
    max_attempts: int
    keys: list[KeyRuntime]
    state: object  # services.jobs.RequestState
    seq: int
    waiting: list[Task] = field(default_factory=list)
    pending: int = 0  # tasks not final yet
    running: int = 0
    excluded: dict[int, str] = field(default_factory=dict)  # key_id -> "auth" | "daily" | "inactive"
    cancelled: bool = False  # stopped by the user (D-14.2): nothing new starts, running calls finish


class Handler(Protocol):
    """What the JobManager does at each step (persistence, status, results)."""

    def started(self, task: Task, key: KeyRuntime) -> None: ...
    def call(self, task: Task, key: KeyRuntime) -> AnalysisOutput: ...
    def attempt_done(self, task: Task, seconds: float, input_tokens: int | None,
                     output_tokens: int | None, answered: bool) -> None: ...  # D-14.5, D-14.6
    def stopped(self, task: Task) -> None: ...  # D-14.2
    def succeeded(self, task: Task, output: AnalysisOutput) -> None: ...
    def failed(self, task: Task, reason: str) -> None: ...
    def retry_wait(self, task: Task, reason: str) -> None: ...
    def waiting(self, task: Task, reason: str) -> None: ...  # called under the scheduler lock: keep it in-memory
    def key_error(self, key_id: int, message: str) -> None: ...
    def rejected(self, task: Task, error: LlmError) -> None: ...
    def request_done(self, run: Run) -> None: ...


# --------------------------------------------------------------------------- status snapshot (D-11.14)
@dataclass(frozen=True)
class SlotView:
    key_label: str
    model_label: str
    running: int
    concurrency: int
    starts_last_minute: int
    rpm: int | None
    used_today: int
    rpd: int | None
    cooldown_s: float
    active: bool


@dataclass(frozen=True)
class RunView:
    request_id: str
    username: str
    model_label: str
    running: int
    waiting: int
    retry_waiting: int
    pending: int


@dataclass(frozen=True)
class EngineView:
    cap: int
    running: int
    slots: list[SlotView]
    runs: list[RunView]


def failure_reason(error: LlmError | None, attempts: int, excluded: dict[int, str]) -> str:
    n = fa_digits(attempts)
    if error is None:
        if excluded and all(why == "daily" for why in excluded.values()):
            return "سقف روزانه همه کلیدهای انتخاب‌شده برای این مدل پر شده است."
        if excluded and all(why == "inactive" for why in excluded.values()):
            return "کلیدها یا مدل این درخواست غیرفعال یا حذف شده‌اند."
        return "هیچ کلید قابل استفاده‌ای باقی نماند (کلیدها نامعتبر، غیرفعال یا سقف روزانه‌شان پر است)."
    detail = str(error)
    return {
        ErrorKind.RATE_LIMIT: f"محدودیت نرخ ارائه‌دهنده (۴۲۹) پس از {n} تلاش. {detail}",
        ErrorKind.TRANSIENT: f"خطای موقت سرور یا شبکه پس از {n} تلاش. {detail}",
        ErrorKind.INVALID_OUTPUT: f"پاسخ مدل پس از {n} تلاش معتبر نبود. {detail}",
        ErrorKind.AUTH: f"کلیدهای انتخاب‌شده نامعتبر یا فاقد مجوز هستند (۴۰۱/۴۰۳). {detail}",
        ErrorKind.TOO_LARGE: f"حجم سند برای این مدل بیش از حد مجاز است. {detail}",
        ErrorKind.PERMANENT: f"خطای غیرقابل تکرار: {detail}",
    }[error.kind]


class Scheduler:
    def __init__(self, handler: Handler, *, backoff: tuple[float, ...] = RETRY_BACKOFF_S, clock=time.monotonic):
        self.handler = handler
        self.backoff = backoff
        self.clock = clock
        self._cond = threading.Condition()
        self._runs: dict[str, Run] = {}
        # Turns (D-11.12): the user served least recently goes first; a newcomer (0) goes first.
        self._last_served: dict[int, int] = {}
        self._turn = 0
        self._slots: dict[tuple[int, int], Slot] = {}
        self._running = 0
        self._cap = max_parallel_calls()
        self._seq = 0
        self._next_refresh = 0.0
        self._warned_daily: set[tuple[int, int, str]] = set()
        self._stopped = False
        # Threads only run real calls, so the pool never needs more threads than the highest cap.
        self._executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_CALLS_RANGE[1], thread_name_prefix="fda-call")
        self._thread = threading.Thread(target=self._loop, name="fda-scheduler", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ public
    def add_run(self, *, request_id: str, user_id: int, username: str, model_id: int, model_label: str,
                max_attempts: int, keys: list[KeyRuntime], state: object, docs: list) -> None:
        """Queue documents of a request. `keys` must be non-empty (checked by the caller)."""
        pairs = {(k.key_id, model_id) for k in keys}
        counts = used_today(pairs - set(self._slots))  # DB read outside the lock
        with self._cond:
            self._seq += 1
            run = Run(request_id, user_id, username, model_id, model_label, max_attempts, keys, state, self._seq)
            for key in keys:
                pair = (key.key_id, model_id)
                slot = self._slots.get(pair)
                if slot is None:
                    slot = self._slots[pair] = Slot(key.key_id, model_id, key.label, model_label, key.limits,
                                                    day=utc_day(), used=counts.get(pair, 0))
                slot.key_label, slot.model_label, slot.limits = key.label, model_label, key.limits
            run.waiting = [Task(run, doc, doc.position) for doc in docs]
            run.pending = len(run.waiting)
            self._runs[request_id] = run
            self._last_served.setdefault(user_id, 0)
            self._cond.notify_all()
        log.info("Queued %s: user=%s model=%s keys=%s documents=%d", request_id, username, model_label,
                 [k.key_id for k in keys], len(docs))

    def cancel(self, request_id: str) -> bool:
        """Stop a request (D-14.2): waiting documents become stopped; running calls finish and are kept.

        Returns False if the request is not in the scheduler (already finished).
        """
        with self._cond:
            run = self._runs.get(request_id)
            if run is None:
                return False
            run.cancelled = True
            stopped, run.waiting = run.waiting, []
            self._cond.notify_all()
        log.info("Stop requested for %s: %d waiting document(s) stopped, %d call(s) still running", request_id,
                 len(stopped), run.running)
        for task in stopped:
            self._safely(self.handler.stopped, task)
            self._close_task(task)
        return True

    def stop(self) -> None:
        """Stop planning (tests). Calls already running finish on their own."""
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._executor.shutdown(wait=False)

    def wake(self, refresh: bool = False) -> None:
        """Re-plan now (e.g. after the admin changed the server cap or a key's limits)."""
        with self._cond:
            if refresh:
                self._next_refresh = 0.0
            self._cond.notify_all()

    def is_active(self, request_id: str) -> bool:
        with self._cond:
            return request_id in self._runs

    def snapshot(self) -> EngineView:
        now = self.clock()
        with self._cond:
            slots = []
            for slot in self._slots.values():
                slot.state(now)  # prunes the window, rolls the day
                slots.append(SlotView(slot.key_label, slot.model_label, slot.running, slot.limits.concurrency,
                                      len(slot.starts), slot.limits.rpm, slot.used, slot.limits.rpd,
                                      max(0.0, slot.cooldown_until - now), slot.limits.active))
            runs = [RunView(r.request_id, r.username, r.model_label, r.running,
                            sum(1 for t in r.waiting if t.not_before <= now),
                            sum(1 for t in r.waiting if t.not_before > now), r.pending)
                    for r in self._runs.values()]
            return EngineView(self._cap, self._running, slots, runs)

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        while not self._stopped:
            try:
                self._refresh_if_due()
                with self._cond:
                    if self._stopped:
                        return
                    now = self.clock()
                    starts, finals, wake_in = self._plan(now)
                    if not (starts or finals):
                        self._cond.wait(timeout=min(wake_in, max(0.05, self._next_refresh - now)))
                        continue
                for task, reason in finals:  # may upload a kept PDF: not on the scheduler thread
                    self._executor.submit(self._finalize, task, None, reason)
                for task, key, slot in starts:
                    self._executor.submit(self._work, task, key, slot)
            except Exception:  # noqa: BLE001 - the scheduler must keep running
                log.exception("Scheduler pass failed")
                time.sleep(1.0)

    def _refresh_if_due(self) -> None:
        if self.clock() < self._next_refresh:
            return
        with self._cond:
            pairs = set(self._slots)
        try:
            limits, counts, cap = live_limits(pairs), used_today(pairs), max_parallel_calls()
        except Exception:  # noqa: BLE001 - keep the last known values
            log.exception("Could not refresh limits")
            self._next_refresh = self.clock() + REFRESH_S
            return
        with self._cond:
            for pair, slot in self._slots.items():
                if pair in limits:
                    slot.limits = limits[pair]
                if pair in counts:
                    slot.day, slot.used = utc_day(), counts[pair]
            if cap != self._cap:
                log.info("Server cap on simultaneous calls: %d -> %d", self._cap, cap)
            self._cap = cap
            self._next_refresh = self.clock() + REFRESH_S
            self._cond.notify_all()

    def _plan(self, now: float):
        """One scheduling pass (under the lock). Returns (starts, finals, seconds to next wake)."""
        starts, finals = [], []
        wake_in = REFRESH_S
        blocked: dict[str, str] = {}  # run -> why its ready tasks can't start (keys)

        def user_runs(user_id):
            return [r for r in self._runs.values() if r.user_id == user_id]

        # Turns: one start per user per round, users rotate; repeat while something starts.
        while self._running < self._cap:
            started_any = False
            for user_id in sorted(self._last_served, key=self._last_served.__getitem__):
                if self._running >= self._cap:
                    break
                for run in user_runs(user_id):
                    task = self._next_ready(run, now)
                    if task is None or run.request_id in blocked:
                        continue
                    key, slot, why, seconds, dead = self._choose_key(run, task, now)
                    if key is not None:
                        self._reserve(run, task, key, slot, now)
                        starts.append((task, key, slot))
                        self._turn += 1
                        self._last_served[user_id] = self._turn
                        started_any = True
                        break
                    if dead:  # no usable key left for this request: its waiting documents fail
                        for waiting in list(run.waiting):
                            run.waiting.remove(waiting)
                            finals.append((waiting, failure_reason(waiting.last_error, waiting.attempts, run.excluded)))
                        continue
                    blocked[run.request_id] = why
                    wake_in = min(wake_in, seconds)
            if not started_any:
                break

        server_busy = (f"در انتظار ظرفیت سرور ({fa_digits(self._running)} از {fa_digits(self._cap)} "
                       "درخواست هم‌زمان در حال اجرا)")
        for run in self._runs.values():
            for task in run.waiting:
                if task.not_before > now:
                    wake_in = min(wake_in, task.not_before - now)
                    continue
                reason = blocked.get(run.request_id, server_busy)
                if reason != task.reason:
                    task.reason = reason
                    self.handler.waiting(task, reason)
        return starts, finals, max(0.05, wake_in)

    @staticmethod
    def _next_ready(run: Run, now: float) -> Task | None:
        ready = [t for t in run.waiting if t.not_before <= now]
        return min(ready, key=lambda t: t.position) if ready else None

    def _choose_key(self, run: Run, task: Task, now: float):
        """(key, slot, why-not, seconds-to-wait, no-key-left) for the next call of `run`."""
        candidates, best = [], (math.inf, None)
        for key in run.keys:
            if key.key_id in run.excluded:
                continue
            slot = self._slots[(key.key_id, run.model_id)]
            block, seconds = slot.state(now)
            if block is Block.NONE:
                candidates.append((key, slot))
            elif block in (Block.DAILY, Block.INACTIVE):
                run.excluded[key.key_id] = block.value
                if block is Block.DAILY and (marker := (key.key_id, run.model_id, slot.day)) not in self._warned_daily:
                    self._warned_daily.add(marker)
                    log.warning("Daily limit reached: key=%s model=%s rpd=%s", key.label, run.model_label,
                                slot.limits.rpd)
            elif seconds < best[0] or best[1] is None:
                best = (seconds, self._why(block, slot, seconds))
        if candidates:
            # least loaded first; avoid repeating the key that just failed for this document
            key, slot = min(candidates, key=lambda ks: (ks[1].running / ks[1].limits.concurrency,
                                                        ks[0].key_id == task.last_key))
            return key, slot, None, 0.0, False
        if not any(k.key_id not in run.excluded for k in run.keys):
            return None, None, None, math.inf, True
        return None, None, best[1], best[0], False

    @staticmethod
    def _why(block: Block, slot: Slot, seconds: float) -> str:
        label = slot.key_label
        if block is Block.COOLDOWN:
            return f"کلید «{label}» پس از خطای ۴۲۹ موقتاً متوقف است؛ ادامه از حدود ساعت {clock_time_in(seconds)}"
        if block is Block.RPM:
            return (f"سقف درخواست دقیقه‌ای کلید «{label}» ({fa_digits(slot.limits.rpm)} در دقیقه) پر است؛ "
                    f"ادامه از حدود ساعت {clock_time_in(seconds)}")
        return (f"در انتظار ظرفیت کلید «{label}» ({fa_digits(slot.running)} از "
                f"{fa_digits(slot.limits.concurrency)} درخواست هم‌زمان در حال اجرا)")

    def _reserve(self, run: Run, task: Task, key: KeyRuntime, slot: Slot, now: float) -> None:
        run.waiting.remove(task)
        slot.running += 1
        slot.starts.append(now)
        run.running += 1
        self._running += 1
        task.attempts += 1
        task.last_key = key.key_id
        task.reason = None

    # ------------------------------------------------------------------ worker
    def _work(self, task: Task, key: KeyRuntime, slot: Slot) -> None:
        run = task.run
        output, error, answered = None, None, False
        began = self.clock()
        log.info("Call %s/%s key=%s attempt=%d running=%d/%d", run.request_id, task.position, key.label,
                 task.attempts, self._running, self._cap)
        try:
            self.handler.started(task, key)
            began = self.clock()  # model time only (D-14.5)
            output = self.handler.call(task, key)
            answered = True
        except LlmError as exc:
            error, answered = exc, exc.answered  # an unusable answer was still produced (and paid)
        except Exception as exc:  # noqa: BLE001 - unknown SDK error: treat as transient
            error = LlmError(ErrorKind.TRANSIENT, short_error(exc))
        seconds = self.clock() - began
        source = output if output is not None else error
        if answered:
            try:
                record_usage(key.key_id, run.model_id, source.input_tokens, source.output_tokens)
            except Exception:  # noqa: BLE001 - usage stats must not fail the document
                log.exception("Could not record usage for key %s", key.key_id)
        self._safely(self.handler.attempt_done, task, seconds, source.input_tokens if answered else None,
                     source.output_tokens if answered else None, answered)

        retry_reason = final_reason = None  # a retry_reason means: back to the queue
        with self._cond:
            now = self.clock()
            slot.running -= 1
            run.running -= 1
            self._running -= 1
            if answered:
                slot.used += 1
            if error is not None:
                task.last_error = error
                if error.kind is ErrorKind.RATE_LIMIT:
                    pause = error.retry_after or DEFAULT_COOLDOWN_S
                    slot.cooldown_until = max(slot.cooldown_until, now + pause)
                    log.warning("Cooldown %.0fs: key=%s model=%s (429)", pause, key.label, run.model_label)
                if error.kind is ErrorKind.AUTH:
                    run.excluded[key.key_id] = "auth"
                    task.attempts -= 1  # not the document's fault; bounded by the number of keys
                    retry_reason = f"کلید «{key.label}» نامعتبر یا فاقد مجوز بود؛ تحلیل با کلید دیگری ادامه می‌یابد."
                    log.warning("Key excluded for %s: key=%s (%s)", run.request_id, key.label, error)
                elif error.retryable and task.attempts < run.max_attempts:
                    delay = self.backoff[min(task.attempts - 1, len(self.backoff) - 1)] if self.backoff else 0
                    task.not_before = now + delay
                    retry_reason = (f"تلاش {fa_digits(task.attempts)} ناموفق بود؛ تلاش مجدد از حدود ساعت "
                                    f"{clock_time_in(delay)} ({error})")
                else:
                    final_reason = failure_reason(error, task.attempts, run.excluded)
            self._cond.notify_all()

        log.info("Done %s/%s key=%s outcome=%s in %.1fs tokens=%s/%s", run.request_id, task.position, key.label,
                 "ok" if error is None else error.kind, seconds,
                 getattr(source, "input_tokens", None), getattr(source, "output_tokens", None))
        if error is not None:
            # The status column shows a short message; the provider's full answer goes to the log.
            cause = error.__cause__
            log.warning("Attempt %d failed %s/%s key=%s kind=%s: %s", task.attempts, run.request_id, task.position,
                        key.label, error.kind, f"{type(cause).__name__}: {cause}" if cause else error)
        if error is not None and error.kind is ErrorKind.AUTH:
            self._safely(self.handler.key_error, key.key_id, str(error))
        if error is not None and error.raw_text is not None:  # keep the unusable answer (D-11.18)
            self._safely(self.handler.rejected, task, error)
        if retry_reason is not None and run.cancelled:  # stopped meanwhile: no further attempt (D-14.2)
            self._safely(self.handler.stopped, task)
            self._close_task(task)
        elif retry_reason is not None:
            # Only this thread owns the document until it is queued again, so no newer attempt can
            # start before its "queued" status is written (and then be overwritten by it).
            task.reason = retry_reason
            self._safely(self.handler.retry_wait, task, retry_reason)
            with self._cond:
                if run.cancelled:  # stopped while the retry status was written
                    requeue = False
                else:
                    run.waiting.append(task)
                    requeue = True
                self._cond.notify_all()
            if not requeue:
                self._safely(self.handler.stopped, task)
                self._close_task(task)
        elif error is None or final_reason is not None:
            self._finalize(task, output, final_reason)

    def _finalize(self, task: Task, output: AnalysisOutput | None, reason: str | None) -> None:
        """Hand a final outcome to the JobManager, then close the request when it was the last one."""
        try:
            if output is not None:
                self.handler.succeeded(task, output)
            else:
                self.handler.failed(task, reason or "پردازش این سند کامل نشد.")
        except Exception as exc:  # noqa: BLE001 - isolate every document (D-5.4)
            log.exception("Finalizing %s/%s failed", task.run.request_id, task.position)
            self._safely(self.handler.failed, task, f"خطای داخلی: {short_error(exc)}")
        self._close_task(task)

    def _close_task(self, task: Task) -> None:
        """A document reached a final state; close the request when it was the last one."""
        run, done = task.run, False
        with self._cond:
            run.pending -= 1
            if run.pending <= 0 and run.running == 0 and not run.waiting:
                self._runs.pop(run.request_id, None)
                if not any(r.user_id == run.user_id for r in self._runs.values()):
                    self._last_served.pop(run.user_id, None)
                done = True
            self._cond.notify_all()
        if done:
            log.info("Request %s finished", run.request_id)
            self._safely(self.handler.request_done, run)

    @staticmethod
    def _safely(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("Engine callback %s failed", getattr(fn, "__name__", fn))
