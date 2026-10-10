"""Scheduler (D-11.8 .. D-11.14): capacity rules, real parallelism, turns, daily limits, wait reasons.

Engine tests run real threads against the mock provider (no network); timings use short delays.
"""

import math
import threading
import time
import types

import pytest

from core.models import Role
from services import app_settings, jobs
from services import providers as pv
from services.auth import ensure_bootstrap_admin, load_active_user
from services.llm import mock
from services.providers import KeyLimits
from services.scheduler import Block, Slot, utc_day
from services.uploads import IncomingFile
from services.users import create_user, default_form


# ----------------------------------------------------------------------------- capacity rules (pure)
def slot(**limits) -> Slot:
    base = {"rpm": None, "rpd": None, "concurrency": 1}
    base.update(limits)
    return Slot(1, 1, "کلید", "مدل", KeyLimits(**base), day=utc_day())


def test_concurrency_slots():
    s = slot(concurrency=3)
    s.running = 2
    assert s.state(0.0) == (Block.NONE, 0.0)
    s.running = 3
    assert s.state(0.0)[0] is Block.BUSY


def test_rpm_window():
    s = slot(rpm=2, concurrency=5)
    s.starts.extend([100.0, 110.0])
    block, seconds = s.state(120.0)
    assert block is Block.RPM and seconds == pytest.approx(40.0)  # the first start leaves the window at 160
    assert s.state(160.0) == (Block.NONE, 0.0)


def test_cooldown_after_429():
    s = slot()
    s.cooldown_until = 130.0
    assert s.state(100.0) == (Block.COOLDOWN, 30.0)
    assert s.state(130.0)[0] is Block.NONE


def test_daily_limit_counts_running_calls():
    s = slot(rpd=3, concurrency=5)
    s.used = 2
    s.running = 1  # the last request of today is already in flight: wait, don't exceed
    assert s.state(0.0)[0] is Block.BUSY
    s.running, s.used = 0, 3
    assert s.state(0.0) == (Block.DAILY, math.inf)
    s.day = "2000-01-01"  # a new UTC day starts from zero
    assert s.state(0.0)[0] is Block.NONE


def test_inactive_key():
    assert slot(active=False).state(0.0)[0] is Block.INACTIVE


# ----------------------------------------------------------------------------- engine
def pdf(name):
    return IncomingFile(name, f"%PDF-1.4 {name}".encode())


@pytest.fixture()
def calls(monkeypatch):
    """Records the order of mock API calls and the peak number running at once."""
    rec = types.SimpleNamespace(active=0, peak=0, order=[], lock=threading.Lock())
    original = mock.analyze_pdf

    def counting(cfg, data, file_name):
        with rec.lock:
            rec.active += 1
            rec.peak = max(rec.peak, rec.active)
            rec.order.append(file_name)
        try:
            return original(cfg, data, file_name)
        finally:
            with rec.lock:
                rec.active -= 1

    monkeypatch.setattr(mock, "analyze_pdf", counting)
    return rec


@pytest.fixture()
def engine(db, tmp_path):
    ensure_bootstrap_admin()
    managers = []

    def setup(delay=0.4, cap=None, fail="", **key):
        if cap is not None:
            app_settings.set_max_parallel_calls(cap)
        provider = pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", f"mock://?delay={delay}&fail_names={fail}"))
        pv.save_model(None, provider.id, pv.ModelForm("mock-1", "مدل آزمایشی"))
        saved = pv.save_key(None, provider.id, pv.KeyForm("کلید ۱", "secret-key-0001", **key))
        pv.set_grant(pv.Subject(role=Role.ANALYST), provider.id, "allow", None, None)
        manager = jobs.JobManager(spool_dir=tmp_path / f"spool{len(managers)}", backoff=(0,))
        managers.append(manager)
        return types.SimpleNamespace(manager=manager, provider=provider, key=saved)

    def user(name):
        form = default_form(Role.ANALYST)
        form.display_name = name
        form.docs_per_hour = form.docs_per_day = 100
        return load_active_user(create_user(1, name, form)[0].id)

    def submit(env, who, files):
        choice = next(c for c in pv.usable_models(who.id) if not c.own)
        return env.manager.submit(who, choice, [k.id for k in choice.keys], files)

    yield types.SimpleNamespace(setup=setup, user=user, submit=submit)
    for manager in managers:
        manager.close()


def test_key_concurrency_is_honoured(engine, calls):
    """Issue 2: a key with concurrency 5 runs 5 documents at the same time."""
    env = engine.setup(delay=0.6, concurrency=5)
    ali = engine.user("ali")
    began = time.monotonic()
    rid = engine.submit(env, ali, [pdf(f"{y}.pdf") for y in range(1398, 1403)])
    assert env.manager.wait_idle(rid, 30)
    assert calls.peak == 5
    assert time.monotonic() - began < 2.5  # one round of calls, not five
    assert [d.status for d in env.manager.get(rid, ali).docs] == ["succeeded"] * 5


def test_server_cap_limits_all_calls(engine, calls):
    env = engine.setup(delay=0.5, cap=2, concurrency=5)
    ali = engine.user("ali")
    rid = engine.submit(env, ali, [pdf(f"{i}.pdf") for i in range(5)])
    time.sleep(0.25)
    waiting = [d for d in env.manager.get(rid, ali).docs if d.status == "queued"]
    assert waiting and all("ظرفیت سرور" in d.reason for d in waiting)
    assert env.manager.wait_idle(rid, 30)
    assert calls.peak == 2


def test_key_capacity_is_shared_by_users(engine, calls):
    env = engine.setup(delay=0.4, concurrency=2)
    ali, sara = engine.user("ali"), engine.user("sara")
    first = engine.submit(env, ali, [pdf(f"a{i}.pdf") for i in range(3)])
    second = engine.submit(env, sara, [pdf(f"s{i}.pdf") for i in range(3)])
    assert env.manager.wait_idle(first, 30) and env.manager.wait_idle(second, 30)
    assert calls.peak == 2


def test_turns_rotate_between_users(engine, calls):
    """D-11.12: a second user's document goes next, not after the first user's whole batch."""
    env = engine.setup(delay=0.3, concurrency=1)
    ali, sara = engine.user("ali"), engine.user("sara")
    first = engine.submit(env, ali, [pdf(f"a{i}.pdf") for i in range(4)])
    time.sleep(0.1)
    second = engine.submit(env, sara, [pdf("s0.pdf")])
    assert env.manager.wait_idle(first, 30) and env.manager.wait_idle(second, 30)
    assert calls.order[:3] == ["a0.pdf", "s0.pdf", "a1.pdf"]


def test_waiting_documents_are_not_shown_as_processing(engine, calls):
    """D-11.13: only a running call is "processing"; the others say what they wait for."""
    env = engine.setup(delay=0.8, concurrency=1)
    ali = engine.user("ali")
    rid = engine.submit(env, ali, [pdf("a.pdf"), pdf("b.pdf")])
    time.sleep(0.3)
    first, second = env.manager.get(rid, ali).docs
    assert first.status == "processing"
    assert second.status == "queued" and "در انتظار ظرفیت کلید" in second.reason and second.started_at is None
    assert env.manager.wait_idle(rid, 30)


def test_daily_limit_is_never_exceeded(engine, calls):
    env = engine.setup(delay=0.3, concurrency=3, rpd=1)
    ali = engine.user("ali")
    rid = engine.submit(env, ali, [pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf")])
    assert env.manager.wait_idle(rid, 30)
    docs = env.manager.get(rid, ali).docs
    assert calls.peak == 1 and len(calls.order) == 1
    assert sorted(d.status for d in docs) == ["failed", "failed", "succeeded"]
    assert all("سقف روزانه" in d.reason for d in docs if d.status == "failed")


def test_limit_changes_apply_without_restart(engine, calls):
    """D-11.9: raising a key's concurrency affects documents already queued."""
    env = engine.setup(delay=0.8, concurrency=1)
    ali = engine.user("ali")
    rid = engine.submit(env, ali, [pdf(f"{i}.pdf") for i in range(4)])
    time.sleep(0.2)
    pv.save_key(None, env.provider.id, pv.KeyForm("کلید ۱", "", concurrency=4), env.key.id)
    env.manager.scheduler.wake(refresh=True)
    assert env.manager.wait_idle(rid, 30)
    assert calls.peak >= 3


def test_engine_status_snapshot(engine, calls):
    env = engine.setup(delay=0.6, concurrency=2, rpm=10)
    ali = engine.user("ali")
    rid = engine.submit(env, ali, [pdf(f"{i}.pdf") for i in range(3)])
    time.sleep(0.2)
    view = env.manager.scheduler.snapshot()
    assert view.cap == 20 and view.running == 2  # default server cap (D-11.11)
    (key,) = view.slots
    assert (key.running, key.concurrency, key.starts_last_minute, key.rpm) == (2, 2, 2, 10)
    (run,) = view.runs
    assert (run.request_id, run.username, run.running, run.waiting) == (rid, "ali", 2, 1)
    assert env.manager.wait_idle(rid, 30)
    assert env.manager.scheduler.snapshot().runs == []


def test_server_cap_setting_is_validated(db):
    assert app_settings.max_parallel_calls() == 20
    app_settings.set_max_parallel_calls(35)
    assert app_settings.max_parallel_calls() == 35
    for bad in (0, 101):
        with pytest.raises(app_settings.SettingError):
            app_settings.set_max_parallel_calls(bad)


def test_final_status_is_never_overwritten_by_an_earlier_attempt(engine, calls):
    """Immediate retries on several threads: each document ends failed after exactly 3 attempts."""
    env = engine.setup(delay=0, fail="bad", concurrency=4)
    ali = engine.user("ali")
    for round_no in range(3):
        rid = engine.submit(env, ali, [pdf(f"bad{round_no}-{i}.pdf") for i in range(8)])
        assert env.manager.wait_idle(rid, 30)
        docs = env.manager.get(rid, ali).docs
        assert [(d.status, d.attempts) for d in docs] == [("failed", 3)] * 8
        assert all("پس از ۳ تلاش" in d.reason for d in docs)
