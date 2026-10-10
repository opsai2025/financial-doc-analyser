"""End-to-end engine tests with the mock provider (no network, no API cost)."""

import pytest
from sqlalchemy import func, select

from core.db import session_scope
from core.models import AnalysisRequest, AnalysisResult, RequestDocument, Role, UsageEvent, User, UserDocument
from services import jobs
from services import providers as pv
from services.auth import ensure_bootstrap_admin, load_active_user
from services.uploads import IncomingFile
from services.users import create_user, default_form


def pdf(name, salt=""):
    return IncomingFile(name, f"%PDF-1.4 {name}{salt}".encode())


@pytest.fixture()
def env(db, tmp_path):
    ensure_bootstrap_admin()
    provider = pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", "mock://?delay=0&fail_names=bad"))
    model = pv.save_model(None, provider.id, pv.ModelForm("mock-1", "مدل آزمایشی", max_attempts=3))
    key1 = pv.save_key(None, provider.id, pv.KeyForm("کلید ۱", "secret-key-0001"))
    key2 = pv.save_key(None, provider.id, pv.KeyForm("کلید ۲", "secret-key-0002"))
    pv.set_grant(pv.Subject(role=Role.ANALYST), provider.id, "allow", None, None)
    pv.set_grant(pv.Subject(role=Role.GUEST), provider.id, "allow", None, None)
    manager = jobs.JobManager(spool_dir=tmp_path / "spool", backoff=(0,))

    def user(name, role, **quota):
        form = default_form(role)
        form.display_name = name
        for k, v in quota.items():
            setattr(form, k, v)
        created = create_user(1, name, form)[0]
        return load_active_user(created.id)

    def choice(u, own=False):
        return next(c for c in pv.usable_models(u.id) if c.own == own)

    def run(u, files, **kw):
        c = kw.pop("choice", None) or choice(u)
        rid = manager.submit(u, c, [k.id for k in c.keys], files, **kw)
        assert manager.wait_idle(rid, 30)
        return manager.get(rid, u)

    class Env:
        pass

    e = Env()
    e.__dict__.update(provider=provider, model=model, keys=(key1, key2), manager=manager, user=user, run=run,
                      choice=choice)
    yield e
    manager.close()


def statuses(state):
    return [d.status for d in state.docs]


def test_batch_with_duplicate_and_failure_isolation(env):
    ali = env.user("ali", Role.ANALYST)
    state = env.run(ali, [pdf("1398.pdf"), pdf("bad.pdf"), pdf("copy.pdf", ""), pdf("1399.pdf")])
    # copy.pdf has different bytes (name in content) -> not a duplicate; make a real duplicate:
    assert statuses(state) == ["succeeded", "failed", "succeeded", "succeeded"]
    bad = state.docs[1]
    assert bad.attempts == 3 and "تلاش" in bad.reason  # transient mock failure retried up to max_attempts
    with session_scope() as db:
        assert db.get(AnalysisRequest, state.id).status == "completed"
        assert db.scalar(select(func.count()).select_from(AnalysisResult)) == 3
        assert db.scalar(select(func.count()).select_from(UsageEvent)) == 3  # failures are free (D-4.4)
        assert db.scalar(select(func.count()).select_from(UserDocument).where(UserDocument.user_id == ali.id)) == 4
    assert state.docs[0].summary["year"] == 1398


def test_duplicates_within_batch(env):
    ali = env.user("ali", Role.ANALYST)
    same = pdf("a.pdf")
    state = env.run(ali, [same, IncomingFile("b.pdf", same.data)])
    assert statuses(state) == ["succeeded", "duplicate"]
    assert "a.pdf" in state.docs[1].reason


def test_quota_skip_then_retry_same_request(env):
    ali = env.user("ali", Role.ANALYST, docs_per_hour=2, docs_per_day=10)
    state = env.run(ali, [pdf("1.pdf"), pdf("2.pdf"), pdf("3.pdf")])
    assert statuses(state) == ["succeeded", "succeeded", "skipped_quota"]
    with session_scope() as db:
        db.get(User, ali.id).docs_per_hour = 5
    assert env.manager.retry(state.id, ali) == 1
    env.manager.wait_idle(state.id, 30)
    state = env.manager.get(state.id, ali)
    assert statuses(state) == ["succeeded", "succeeded", "succeeded"]
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AnalysisRequest)) == 1  # same request id (D-5.5)


def test_reuse_previous_result_costs_nothing(env):
    ali = env.user("ali", Role.ANALYST)
    first = env.run(ali, [pdf("1400.pdf")])
    doc_id = first.docs[0].document_id
    versions = env.manager.previous_versions(ali, [doc_id])
    assert [v.request_id for v in versions[doc_id]] == [first.id]
    second = env.run(ali, [pdf("1400.pdf")], reuse={doc_id: first.id})
    assert statuses(second) == ["reused"] and second.docs[0].result_request_id == first.id
    assert second.docs[0].summary["year"] == 1400
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(UsageEvent)) == 1


def test_guest_results_in_memory_only(env):
    guest = env.user("gst", Role.GUEST, guest_doc_cap=2)
    state = env.run(guest, [pdf("1.pdf"), pdf("2.pdf"), pdf("3.pdf")])
    assert statuses(state) == ["succeeded", "succeeded", "skipped_quota"]
    assert all(d.data for d in state.docs[:2])
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AnalysisRequest)) == 0
        assert db.scalar(select(func.count()).select_from(AnalysisResult)) == 0
        assert db.scalar(select(func.count()).select_from(UsageEvent)) == 2  # tiny usage rows only (D-4.3)
    assert env.manager.latest_ephemeral(guest.id).id == state.id
    with session_scope() as db:
        db.get(User, guest.id).guest_doc_cap = 5
    newer = env.run(guest, [pdf("4.pdf")])
    assert env.manager.latest_ephemeral(guest.id).id == newer.id  # last request replaces the previous one
    assert env.manager.get(state.id, guest) is None


def test_db_quota_full_keeps_result_in_memory(env):
    ali = env.user("ali", Role.ANALYST, db_quota_mb=0)
    state = env.run(ali, [pdf("1.pdf")])
    doc = state.docs[0]
    assert doc.status == "succeeded" and not doc.stored and doc.data is not None
    assert "سهمیه ذخیره نتایج" in doc.reason
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(AnalysisResult)) == 0
        assert db.scalar(select(func.count()).select_from(UsageEvent)) == 1


def test_storage_quota_full_does_not_block(env):
    ali = env.user("ali", Role.ANALYST, storage_quota_mb=0)
    state = env.run(ali, [pdf("1.pdf")])
    assert state.docs[0].status == "succeeded" and "PDF" in state.docs[0].reason
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(UserDocument)) == 0


def test_own_keys_waive_doc_quota(env):
    ali = env.user("ali", Role.ANALYST, docs_per_hour=0, docs_per_day=0)
    own = pv.save_provider(ali.id, pv.ProviderForm("شخصی", "mock", "mock://?delay=0"))
    pv.save_model(ali.id, own.id, pv.ModelForm("own"))
    pv.save_key(ali.id, own.id, pv.KeyForm("من", "own-key-1234"))
    assert statuses(env.run(ali, [pdf("1.pdf")])) == ["skipped_quota"]
    assert statuses(env.run(ali, [pdf("2.pdf")], choice=env.choice(ali, own=True))) == ["succeeded"]


def test_auth_errors_exclude_keys(env):
    ali = env.user("ali", Role.ANALYST)
    pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", "mock://?delay=0&fail_rate=1&fail_kind=auth"), env.provider.id)
    state = env.run(ali, [pdf("1.pdf")])
    doc = state.docs[0]
    assert doc.status == "failed" and doc.attempts == 0 and "کلید" in doc.reason


def test_interrupted_recovery_and_retry_from_storage(env, tmp_path):
    ali = env.user("ali", Role.ANALYST)
    state = env.run(ali, [pdf("bad.pdf")])  # fails; PDF kept in storage
    with session_scope() as db:
        db.execute(RequestDocument.__table__.update().values(status="processing"))
        db.execute(AnalysisRequest.__table__.update().values(status="running"))
    fresh = jobs.JobManager(spool_dir=tmp_path / "other-spool", backoff=(0,))  # simulates a new process
    assert fresh.recover_interrupted() == 1
    loaded = fresh.get(state.id, ali)
    assert loaded.status == "interrupted" and statuses(loaded) == ["interrupted"]
    pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", "mock://?delay=0"), env.provider.id)
    assert fresh.retry(state.id, ali) == 1  # PDF fetched back from storage
    fresh.wait_idle(state.id, 30)
    assert statuses(fresh.get(state.id, ali)) == ["succeeded"]
    fresh.close()


def test_access_checks(env):
    ali = env.user("ali", Role.ANALYST)
    sara = env.user("sara", Role.ANALYST)
    viewer = env.user("viw", Role.VIEWER)
    state = env.run(ali, [pdf("1.pdf")])
    assert env.manager.get(state.id, sara) is None
    with pytest.raises(jobs.JobError):
        env.manager.submit(viewer, env.choice(ali), [env.keys[0].id], [pdf("x.pdf")])
    with pytest.raises(jobs.JobError):
        env.manager.submit(ali, env.choice(ali), [999], [pdf("x.pdf")])


def test_recent_requests_show_creator_to_admin_only(env):
    """Req. 2: admins see who created each request; analysts only see their own requests."""
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    first = env.run(ali, [pdf("1398.pdf")])
    second = env.run(sara, [pdf("1399.pdf")])
    admin = load_active_user(1)
    rows = env.manager.recent_requests(admin)
    assert [(r.id, name) for r, name in rows] == [(second.id, "sara"), (first.id, "ali")]
    assert [r.id for r, _ in env.manager.recent_requests(ali)] == [first.id]
    assert env.manager.get(first.id, admin).username == "ali"
    assert jobs.JobManager._load(first.id).username == "ali"  # also after a restart (from the DB)


def test_paid_result_survives_a_failed_save(env, monkeypatch):
    """D-11.16: if writing the result to the DB fails, the document still succeeds with its result kept."""
    ali = env.user("ali", Role.ANALYST)

    def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(jobs.results, "save_result", broken)
    state = env.run(ali, [pdf("1398.pdf")])
    doc = state.docs[0]
    assert doc.status == "succeeded" and not doc.stored and doc.data is not None
    assert "ذخیره نتیجه در پایگاه داده" in doc.reason
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(UsageEvent)) == 1  # still counted
        assert db.scalar(select(func.count()).select_from(AnalysisResult)) == 0


def _mock_url(env, query):
    pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", f"mock://?delay=0&{query}"), env.provider.id)


def test_extra_text_after_json_accepted_with_warning(env):
    """D-11.18: the first JSON is used, the document is flagged and the whole answer is kept."""
    from core.models import RawOutput

    ali = env.user("ali", Role.ANALYST)
    _mock_url(env, "extra_names=1398")
    state = env.run(ali, [pdf("1398.pdf"), pdf("1399.pdf")])
    flagged, clean = state.docs
    assert flagged.status == clean.status == "succeeded" and flagged.stored
    assert [w["code"] for w in flagged.warnings] == ["extra_text_after_json"] and clean.warnings is None
    assert "متن اضافه" in flagged.reason
    with session_scope() as db:
        assert db.get(AnalysisResult, (state.id, flagged.document_id)).warnings[0]["code"] == "extra_text_after_json"
        (raw,) = db.scalars(select(RawOutput)).all()
        assert raw.kind == "accepted" and raw.document_id == flagged.document_id
        stored_row = db.get(RequestDocument, flagged.row_id)
        assert stored_row.warnings[0]["code"] == "extra_text_after_json"
    (answer,) = env.manager.raw_answers(state, flagged)
    assert answer["text"].endswith("توضیح تکمیلی مدل پس از JSON.")


def test_unusable_answers_are_kept_and_counted(env):
    """D-11.18: every rejected answer is stored verbatim; its tokens count towards usage and RPD."""
    from core.models import KeyUsageDaily, RawOutput
    from services.quota import db_used_bytes

    ali = env.user("ali", Role.ANALYST)
    _mock_url(env, "broken_names=1398")
    state = env.run(ali, [pdf("1398.pdf")])
    doc = state.docs[0]
    assert doc.status == "failed" and doc.attempts == 3 and "JSON" in doc.reason
    assert [w["code"] for w in doc.warnings] == ["rejected_answer_kept"] * 3
    with session_scope() as db:
        rows = db.scalars(select(RawOutput).order_by(RawOutput.attempt)).all()
        assert [(r.kind, r.attempt) for r in rows] == [("rejected", 1), ("rejected", 2), ("rejected", 3)]
        assert all(r.input_tokens and r.output_tokens == 6000 for r in rows)
        usage = db.scalars(select(KeyUsageDaily)).all()
        assert sum(u.requests for u in usage) == 3 and sum(u.output_tokens for u in usage) == 18_000
        assert db_used_bytes(db, ali.id) == sum(r.size_bytes for r in rows)  # counted in the results quota
        assert db.scalar(select(func.count()).select_from(UsageEvent)) == 0  # failures don't use document quota
    answers = env.manager.raw_answers(state, doc)
    assert len(answers) == 3 and all(a["text"].startswith('{"تحلیل_جامع_گزارش_حسابرسی"') for a in answers)


def test_guest_rejected_answers_stay_in_memory(env):
    from core.models import RawOutput

    guest = env.user("gst", Role.GUEST, guest_doc_cap=5)
    _mock_url(env, "broken_names=bad&extra_names=ok")
    state = env.run(guest, [pdf("bad.pdf"), pdf("ok.pdf")])
    bad, ok = state.docs
    assert bad.status == "failed" and len(bad.raw_answers) == 3
    assert ok.status == "succeeded" and ok.warnings and len(ok.raw_answers) == 1
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(RawOutput)) == 0  # guests: nothing in the DB (D-4.3)
