"""Round 1 refinements (D-14): stop, re-run, time and tokens per document, PDFs, sharing with analysts."""

import io
import time
import zipfile

import pytest
from sqlalchemy import select, update
from test_jobs import _mock_url, env, pdf, statuses  # noqa: F401  (shared engine fixture)

from core.db import session_scope
from core.models import AnalysisRequest, ApiKey, RequestDocument, Role
from services import dashboard as dash
from services import history, jobs
from services import providers as pv
from ui import charts


def test_stop_keeps_finished_and_running_results(env):
    """D-14.2: waiting documents are stopped; calls already running finish and keep «موفق»."""
    ali = env.user("ali", Role.ANALYST)
    pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", "mock://?delay=1"), env.provider.id)  # 1 s per call
    with session_scope() as db:  # one call per key at a time, so some documents are still waiting
        db.execute(update(ApiKey).values(concurrency=1))
    choice = env.choice(ali)
    rid = env.manager.submit(ali, choice, [k.id for k in choice.keys], [pdf(f"{y}.pdf") for y in range(1395, 1401)])
    state = env.manager.get(rid, ali)
    for _ in range(100):  # until the first calls are running
        if state.count(jobs.PROCESSING):
            break
        env.manager.scheduler.wake()
        time.sleep(0.05)
    env.manager.stop(rid, ali)
    assert state.status in (jobs.STOPPING, jobs.STOPPED)
    assert env.manager.wait_idle(rid, 30)
    final = env.manager.get(rid, ali)
    assert final.status == jobs.STOPPED
    assert set(statuses(final)) == {jobs.SUCCEEDED, jobs.STOPPED}
    assert final.count(jobs.SUCCEEDED) >= 1 and final.count(jobs.STOPPED) >= 1
    assert all(d.reason == jobs.STOPPED_REASON for d in final.docs if d.status == jobs.STOPPED)
    with session_scope() as db:
        assert db.get(AnalysisRequest, rid).status == jobs.STOPPED
    assert env.manager.reserved(ali.id, False) == 0  # the quota reservation is released


def test_rerun_resumes_unfinished_and_reanalyses_successful_in_a_new_request(env):
    """D-14.3/D-14.4: unfinished → same request; with a result → new linked request; other model → all new."""
    ali = env.user("ali", Role.ANALYST)
    state = env.run(ali, [pdf("1398.pdf"), pdf("bad.pdf"), pdf("1399.pdf")])
    assert statuses(state) == ["succeeded", "failed", "succeeded"]
    _mock_url(env, "delay=0")  # "bad" now succeeds
    choice = env.choice(ali)
    resumed, new_id = env.manager.rerun(state.id, ali, [0, 1], choice, [k.id for k in choice.keys])
    assert resumed == 1 and new_id is not None
    env.manager.wait_idle(state.id, 30)
    env.manager.wait_idle(new_id, 30)
    assert statuses(env.manager.get(state.id, ali)) == ["succeeded", "succeeded", "succeeded"]
    child = env.manager.get(new_id, ali)
    assert [d.file_name for d in child.docs] == ["1398.pdf"] and child.parent_request_id == state.id
    with session_scope() as db:
        assert db.get(AnalysisRequest, new_id).parent_request_id == state.id

    pv.save_model(None, env.provider.id, pv.ModelForm("mock-2", "مدل دوم"))
    other = next(c for c in pv.usable_models(ali.id) if c.model_name == "mock-2")
    resumed, third = env.manager.rerun(state.id, ali, [2], other, [k.id for k in other.keys])
    assert resumed == 0 and third and env.manager.wait_idle(third, 30)
    assert env.manager.get(third, ali).model_name == "mock-2"
    with pytest.raises(jobs.JobError):
        env.manager.rerun(state.id, env.user("sara", Role.ANALYST), [0], choice, [k.id for k in choice.keys])


def test_time_and_tokens_over_all_attempts(env):
    """D-14.5/D-14.6: model time and tokens per attempt, accumulated; no answer → no tokens."""
    ali = env.user("ali", Role.ANALYST)
    _mock_url(env, "delay=0&broken_names=broken&fail_names=bad")
    state = env.run(ali, [pdf("1398.pdf"), pdf("broken.pdf"), pdf("bad.pdf")])
    ok, broken, bad = state.docs
    assert ok.total_input_tokens == ok.input_tokens > 0 and ok.total_output_tokens == ok.output_tokens
    assert ok.last_call_seconds is not None and ok.total_call_seconds >= ok.last_call_seconds
    assert broken.status == "failed" and broken.total_output_tokens == 3 * 6000  # three rejected answers
    assert bad.status == "failed" and bad.attempts == 3 and bad.total_input_tokens == 0
    with session_scope() as db:
        row = db.scalars(select(RequestDocument).where(RequestDocument.id == broken.row_id)).one()
        assert row.total_output_tokens == 18_000 and row.total_call_seconds >= 0
    loaded = jobs.JobManager._load(state.id)
    assert loaded.docs[1].total_output_tokens == 18_000
    # a retry in the same request adds to the totals
    _mock_url(env, "delay=0")
    env.manager.retry(state.id, ali, positions=[1])
    env.manager.wait_idle(state.id, 30)
    again = env.manager.get(state.id, ali).docs[1]
    assert again.status == "succeeded" and again.total_output_tokens > 18_000


def test_pdf_downloads(env):
    """D-14.10: per document from the request, kept PDFs as one file or a ZIP; owners and admins only."""
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    state = env.run(ali, [pdf("1398.pdf"), pdf("1399.pdf")])
    assert env.manager.pdf_available(state) == {d.document_id for d in state.docs}
    assert env.manager.pdf_bytes(state, state.docs[0]) == pdf("1398.pdf").data
    kept = history.kept_files(ali)
    data, name, mime = history.kept_pdfs(ali, [(kept[0].user_id, kept[0].document_id, kept[0].file_name)])
    assert mime == "application/pdf" and data.startswith(b"%PDF") and name == kept[0].file_name
    data, name, mime = history.kept_pdfs(ali, [(f.user_id, f.document_id, f.file_name) for f in kept])
    assert mime == "application/zip" and sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == ["1398.pdf", "1399.pdf"]
    with pytest.raises(history.HistoryError):
        history.kept_pdfs(sara, [(kept[0].user_id, kept[0].document_id, kept[0].file_name)])


def test_dashboard_sources_keep_shared_separate(env):
    """D-14.8: 'all my results' never includes requests shared with me."""
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    mine = env.run(sara, [pdf("1400.pdf")])
    theirs = env.run(ali, [pdf("1398.pdf")])
    history.set_shares(ali, theirs.id, [sara.id])
    assert {r.request_id for r in dash.load_all(sara)} == {mine.id}
    assert {r.request_id for r in dash.load_all(sara, shared=True)} == {theirs.id}


def test_chart_colours_match_between_bars_and_sunburst():
    """D-14.11: one colour per category in both charts; outer ring lighter; full labels wrapped."""
    rows = [{"year": 1398, "c": "ریسک بازار", "s": "نوسان نرخ ارز و اثر آن بر دارایی‌ها و بدهی‌های ارزی"},
            {"year": 1399, "c": "ریسک اعتباری", "s": "تمرکز"}]
    bars = charts.stacked_bars(rows, "c", dash.RISK_CATEGORIES, "t", "a")
    sun = charts.sunburst(rows, "c", "s", "root", dash.RISK_CATEGORIES)
    bar_colors = {t.name: t.marker.color for t in bars.data}
    nodes = dict(zip(sun.data[0].ids, sun.data[0].marker.colors, strict=True))
    assert nodes["c|ریسک بازار"] == bar_colors["ریسک بازار"] and nodes["c|ریسک اعتباری"] == bar_colors["ریسک اعتباری"]
    assert nodes["s|ریسک بازار|" + rows[0]["s"]] != bar_colors["ریسک بازار"]  # lighter shade
    label = sun.data[0].text[list(sun.data[0].ids).index("s|ریسک بازار|" + rows[0]["s"])]
    assert "<br>" in label and label.replace("<br>", " ") == rows[0]["s"]  # full text, wrapped
