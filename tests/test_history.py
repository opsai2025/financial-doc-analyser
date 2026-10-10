"""Step 2b: version picker rules, history search, deletion, kept PDFs, sharing, re-upload, usage (D-12)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_jobs import env, pdf, statuses  # noqa: F401  (shared engine fixture)

from core.db import session_scope
from core.fa import parse_jalali_date
from core.models import AnalysisRequest, AnalysisResult, Document, RawOutput, Role, UserDocument
from services import history, jobs
from services import providers as pv
from services.auth import load_active_user
from services.quota import usage_overview
from services.storage import get_storage


def version(rid, provider, model, minutes):
    return jobs.Version(rid, "d", 1, "u", provider, model, datetime(2026, 10, 9, tzinfo=UTC) + timedelta(minutes=minutes),
                        False)


def test_version_order_and_default():
    versions = [version("old-same", "P", "m", 1), version("other", "P", "x", 9), version("new-same", "P", "m", 5)]
    ordered = jobs.order_versions(versions, "P", "m")
    assert [v.request_id for v in ordered] == ["new-same", "old-same", "other"]
    assert jobs.default_version(versions, "P", "m") == "new-same"
    assert jobs.default_version([version("other", "P", "x", 1)], "P", "m") is None  # -> new analysis (D-5.6)


def test_parse_jalali_date():
    assert parse_jalali_date("۱۴۰۵/۰۷/۱۷") == date(2026, 10, 9)
    assert parse_jalali_date("1403/12/30") == date(2025, 3, 20)  # leap year
    assert parse_jalali_date("1404/12/30") is None
    assert parse_jalali_date("") is None and parse_jalali_date("abc") is None


def second_model(env):
    return pv.save_model(None, env.provider.id, pv.ModelForm("mock-2", "مدل دوم"))


def test_reuse_from_another_model_is_labelled(env):
    ali = env.user("ali", Role.ANALYST)
    first = env.run(ali, [pdf("1398.pdf")])
    second_model(env)
    other = next(c for c in pv.usable_models(ali.id) if c.model_name == "mock-2")
    doc_id = first.docs[0].document_id
    reused = env.run(ali, [pdf("1398.pdf")], choice=other, reuse={doc_id: first.id})
    assert statuses(reused) == ["reused"] and reused.docs[0].source_model == "آزمایشی › mock-1"
    assert jobs.JobManager._load(reused.id).docs[0].source_model == "آزمایشی › mock-1"  # also from the DB


def test_cannot_reuse_someone_elses_result(env):
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    first = env.run(ali, [pdf("1398.pdf")])
    state = env.run(sara, [pdf("1398.pdf")], reuse={first.docs[0].document_id: first.id})
    assert statuses(state) == ["succeeded"]  # analysed anew instead


def test_search_filters(env):
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    good = env.run(ali, [pdf("1398.pdf")])
    bad = env.run(ali, [pdf("bad.pdf")])
    env.run(sara, [pdf("1400.pdf")])
    assert [r.id for r in history.search_requests(ali)] == [bad.id, good.id]
    assert [r.id for r in history.search_requests(ali, term="1398")] == [good.id]
    assert [r.id for r in history.search_requests(ali, term=good.id.lower())] == [good.id]
    # company name, matched on Level-B keys: Arabic kaf/yeh still match (D-7.4)
    assert [r.id for r in history.search_requests(ali, term="بانك نمونه")] == [good.id]
    assert [r.id for r in history.search_requests(ali, statuses={history.HAS_FAILED})] == [bad.id]
    today = datetime.now(UTC).date()
    assert len(history.search_requests(ali, since=today - timedelta(days=1), until=today + timedelta(days=1))) == 2
    assert history.search_requests(ali, until=today - timedelta(days=2)) == []
    admin = load_active_user(1)
    assert len(history.search_requests(admin)) == 3
    assert {r.username for r in history.search_requests(admin, owner_id=sara.id)} == {"sara"}
    row = history.search_requests(ali, term="1398")[0]
    assert (row.total, row.ok, row.failed) == (1, 1, 0) and row.companies == ["بانک نمونه (سهامی عام)"]


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def test_delete_keeps_results_reused_elsewhere(env):
    ali = env.user("ali", Role.ANALYST)
    pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", "mock://?delay=0&extra_names=1398"),
                             env.provider.id)
    first = env.run(ali, [pdf("1398.pdf"), pdf("bad.pdf")])
    doc_id = first.docs[0].document_id
    second = env.run(ali, [pdf("1398.pdf")], reuse={doc_id: first.id})
    with pytest.raises(history.HistoryError):
        history.delete_request(ali, first.id, running=True)
    with pytest.raises(history.HistoryError):
        history.delete_request(env.user("sara", Role.ANALYST), first.id, running=False)

    history.delete_request(ali, first.id, running=False)
    with session_scope() as db:
        assert db.get(AnalysisRequest, first.id) is None
        assert db.get(AnalysisResult, (first.id, doc_id)) is not None  # still used by the second request
        assert count(db, RawOutput, RawOutput.request_id == first.id) == 1  # its accepted answer stays with it
    assert env.manager.get(second.id, ali).docs[0].summary["year"] == 1398

    history.delete_request(ali, second.id, running=False)
    with session_scope() as db:
        assert count(db, AnalysisResult) == 0 and count(db, RawOutput) == 0 and count(db, AnalysisRequest) == 0


def test_kept_files_are_removed_only_when_nobody_keeps_them(env):
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    env.run(ali, [pdf("1398.pdf"), pdf("1399.pdf")])
    env.run(sara, [pdf("1398.pdf")])
    mine = history.kept_files(ali)
    assert sorted(f.file_name for f in mine) == ["1398.pdf", "1399.pdf"] and all(f.requests == 1 for f in mine)
    assert len(history.kept_files(load_active_user(1))) == 3  # admins see everyone's
    shared_doc = next(f for f in mine if f.file_name == "1398.pdf").document_id
    with pytest.raises(history.HistoryError):
        history.delete_kept_files(sara, [(ali.id, shared_doc)])

    history.delete_kept_files(ali, [(f.user_id, f.document_id) for f in mine])
    with session_scope() as db:
        shared = db.get(Document, shared_doc)
        assert shared.storage_path is not None  # sara still keeps it
        only_ali = next(f for f in mine if f.file_name == "1399.pdf").document_id
        assert db.get(Document, only_ali).storage_path is None
        assert count(db, UserDocument, UserDocument.user_id == ali.id) == 0
        assert count(db, AnalysisResult) == 3  # results stay
    assert get_storage().get(f"{shared_doc[:2]}/{shared_doc[2:4]}/{shared_doc}.pdf") is not None
    assert get_storage().get(f"{only_ali[:2]}/{only_ali[2:4]}/{only_ali}.pdf") is None


def test_sharing_with_analysts_and_viewers(env):
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    viewer, guest = env.user("viw", Role.VIEWER), env.user("gst", Role.GUEST)
    state = env.run(ali, [pdf("1398.pdf")])
    assert env.manager.get(state.id, viewer) is None
    assert {o.username for o in history.share_options(ali.id)} == {"sara", "viw"}  # not the owner, no guests
    for wrong in ([ali.id], [guest.id], [1]):  # owner, guest, admin
        with pytest.raises(history.HistoryError):
            history.set_shares(ali, state.id, wrong)
    with pytest.raises(history.HistoryError):
        history.set_shares(sara, state.id, [viewer.id])  # not the owner
    history.set_shares(ali, state.id, [viewer.id, sara.id])  # analysts too (D-14.7)
    assert env.manager.get(state.id, viewer) is not None and env.manager.get(state.id, sara) is not None
    assert [r.id for r in history.search_requests(sara, shared=True)] == [state.id]
    assert history.search_requests(sara) == []  # her own list stays her own
    history.set_shares(ali, state.id, [viewer.id])
    assert env.manager.get(state.id, sara) is None
    assert [r.id for r in history.search_requests(viewer, shared=True)] == [state.id]
    assert history.search_requests(viewer) == []  # a viewer has no requests of their own
    history.set_shares(load_active_user(1), state.id, [])  # admins may manage sharing too
    assert env.manager.get(state.id, viewer) is None
    history.set_shares(ali, state.id, [viewer.id])
    history.delete_request(ali, state.id, running=False)
    assert history.search_requests(viewer, shared=True) == []


def test_reupload_missing_file_then_retry(env):
    ali = env.user("ali", Role.ANALYST, storage_quota_mb=0)  # PDFs are not kept
    state = env.run(ali, [pdf("bad.pdf"), pdf("1398.pdf")])
    env.manager.forget(state.id)  # the processing copy is gone too (e.g. after 7 days)
    state = env.manager.get(state.id, ali)
    missing = env.manager.missing_files(state)
    assert [d.file_name for d in missing] == ["bad.pdf"]
    sara = env.user("sara", Role.ANALYST)
    with pytest.raises(jobs.JobError):
        env.manager.provide_files(state, sara, [pdf("bad.pdf")])
    matched, rejected = env.manager.provide_files(state, ali, [pdf("bad.pdf"), pdf("other.pdf")])
    assert matched == ["bad.pdf"] and rejected == ["other.pdf"] and env.manager.missing_files(state) == []
    pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", "mock://?delay=0"), env.provider.id)
    assert env.manager.retry(state.id, ali) == 1
    env.manager.wait_idle(state.id, 30)
    assert statuses(env.manager.get(state.id, ali)) == ["succeeded", "succeeded"]


def test_usage_overview(env):
    ali = env.user("ali", Role.ANALYST)
    env.run(ali, [pdf("1398.pdf"), pdf("1399.pdf")])
    docs, size = usage_overview()[ali.id]
    with session_scope() as db:
        results = db.scalar(select(func.sum(AnalysisResult.size_bytes)))
        pdfs = db.scalar(select(func.sum(Document.size_bytes)))
    assert docs == 2 and size == results + pdfs
