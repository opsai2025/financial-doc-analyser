"""Admin-editable defaults and limits, usage overview, database-down message (DECISIONS §16)."""

import pytest
from sqlalchemy.exc import OperationalError
from test_jobs import env, pdf  # noqa: F401  (shared engine fixture)
from test_pages_smoke import pages  # noqa: F401  (page runner fixture)

from core.models import Role
from services import app_settings, usage
from services.auth import load_active_user
from services.uploads import from_pdfs
from services.users import default_form


def test_defaults_prefill_new_accounts_only(env):
    ali = env.user("ali", Role.ANALYST)
    app_settings.set_many({"new_docs_per_hour": 3, "new_docs_per_day": 7, "new_guest_doc_cap": 9})
    form = default_form(Role.ANALYST)
    assert (form.docs_per_hour, form.docs_per_day, form.db_quota_mb) == (3, 7, 20)
    assert default_form(Role.GUEST).guest_doc_cap == 9
    assert load_active_user(ali.id).docs_per_hour == 10  # existing accounts keep their values (D-16.4)


def test_settings_validation_saves_nothing_when_invalid(env):
    for bad in ({"max_pdf_mb": 0}, {"max_zip_mb": 301}, {"new_docs_per_hour": 60},  # hour > day (50)
                {"max_pdf_mb": 40, "max_zip_mb": 30}):
        with pytest.raises(app_settings.SettingError):
            app_settings.set_many(bad)
    assert app_settings.upload_limits() == app_settings.UploadLimits()
    with pytest.raises(app_settings.SettingError):
        app_settings.set_many({"max_files_per_batch": 2, "max_pdf_mb": 0})
    assert app_settings.get("max_files_per_batch") == 50  # all or nothing


def test_upload_limits_apply_immediately(env):
    app_settings.set_many({"max_files_per_batch": 2})
    intake = from_pdfs([(f"{i}.pdf", b"%PDF-1 x") for i in range(3)])
    assert len(intake.files) == 2 and "۲ فایل" in intake.rejected[0]


def test_usage_overview_and_admin_warning(env, monkeypatch):
    ali = env.user("ali", Role.ANALYST)
    env.run(ali, [pdf("1398.pdf"), pdf("1399.pdf")])
    system = usage.system_usage()
    assert system.db_bytes > 0 and system.db_limit_bytes == 500 * usage.MB
    assert system.storage_files == 2 and system.storage_bytes > 0
    row = next(u for u in usage.per_user() if u.username == "ali")
    assert (row.requests, row.docs_24h) == (1, 2) and row.results_bytes > 0 and row.pdf_bytes > 0

    assert usage.warnings_for_admin() == []
    monkeypatch.setattr(usage, "database_bytes", lambda: 410 * usage.MB)  # 82 % of 500 MB
    assert usage.warnings_for_admin() == []  # measured at most every 10 minutes
    usage.clear_cache()
    assert [name for name, _ in usage.warnings_for_admin()] == ["db"]


def test_usage_tab_and_banner(env, pages, monkeypatch):
    admin = load_active_user(1)
    monkeypatch.setattr(usage, "database_bytes", lambda: 460 * usage.MB)
    at = pages("settings", admin, settings_tabs=":material/database: مصرف منابع")
    assert any("پایگاه داده" in w.value for w in at.warning)  # the admin banner
    assert any("مصرف کاربران" in h.value for h in at.subheader) and at.dataframe  # the lazy tab ran
    ali = env.user("ali", Role.ANALYST)
    at = pages("analysis", ali)
    assert not any("سقف طرح" in w.value for w in at.warning)  # admins only


def test_database_down_message(env, pages, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from conftest import ROOT
    from ui import session

    pages("analysis", env.user("ali", Role.ANALYST))  # applies the fixture's patches

    def down():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(session, "resolve_user", down)
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
    at.run()
    assert not at.exception
    assert any("Supabase" in e.value for e in at.error)
