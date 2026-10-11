"""Page scripts run without exceptions for each role (Streamlit AppTest, mock provider, test DB)."""

from dataclasses import replace

import pytest
from streamlit.testing.v1 import AppTest
from test_jobs import env, pdf  # noqa: F401  (shared engine fixture)

from conftest import ROOT
from core import logging_setup, migrate
from core.models import Role
from services import history, jobs
from services.auth import load_active_user


@pytest.fixture()
def pages(env, monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_manager", env.manager)  # never touch the real spool folder
    monkeypatch.setattr(logging_setup, "LOG_FILE", tmp_path / "app.log")  # nor the real log
    monkeypatch.setattr(migrate, "upgrade_database", lambda: None)  # the test schema comes from the models

    def run(page, user, **state):
        """Through the real entry script, so navigation (page links, access rules) is exercised too."""
        at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
        at.session_state["user"] = replace(user, must_change_password=False)
        for name, value in state.items():
            at.session_state[name] = value
        at.run()
        at.switch_page(f"app_pages/{page}.py").run()
        assert not at.exception, [e.message for e in at.exception]
        return at

    return run


def test_pages_for_each_role(env, pages):
    ali = env.user("ali", Role.ANALYST)
    viewer = env.user("viw", Role.VIEWER)
    guest = env.user("gst", Role.GUEST, guest_doc_cap=3)
    admin = load_active_user(1)
    state = env.run(ali, [pdf("1398.pdf"), pdf("bad.pdf")])
    history.set_shares(ali, state.id, [viewer.id])
    env.run(guest, [pdf("1399.pdf")])

    at = pages("analysis", ali, an_current=state.id)
    assert any("دانلود اکسل" in b.label for b in at.get("download_button"))
    pages("analysis", guest)
    pages("analysis", admin)
    for user in (ali, viewer, admin):  # the request's section opened: its view loads (D-14.9)
        at = pages("history", user, **{f"hist_exp_{state.id}": True, f"hist_shared_exp_{state.id}": True,
                                        f"hist_rerun_exp_{state.id}": True, f"hist_pdfs_exp_{state.id}": True})
        assert at.dataframe, user.username
    for user in (ali, guest, viewer, admin):
        pages("profile", user)
        at = pages("dashboard", user)
        assert at.get("plotly_chart"), user.username  # the overview charts are drawn
    at = pages("dashboard", ali, dash_open=state.id)
    assert at.selectbox(key="dash_source").value == state.id  # "open in dashboard" preselects the request
    pages("users", admin)
    # Providers list with a provider, a model, a key and the connection test opened (D-14.12)
    model, key = env.model, env.keys[0]
    p = f"admin_prov_p{env.provider.id}"
    at = pages("settings", admin, **{f"{p}_exp": True, f"{p}_m{model.id}_exp": True, f"{p}_k{key.id}_exp": True,
                                     f"{p}_test_exp": True, "admin_prov_add_exp": True})
    assert any(b.label == "ذخیره مدل" for b in at.button) and any(b.label == "ذخیره کلید" for b in at.button)


def test_session_only_login_when_cookie_routes_are_missing(env, pages):
    """A host without the /auth/* routes sends the browser back with the ticket (ui.session)."""
    from services.auth import create_login_ticket

    ali = env.user("ali", Role.ANALYST)
    pages("analysis", ali)  # applies the fixture's patches (no migrations, test log)
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
    at.query_params["ticket"] = create_login_ticket(ali)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.session_state["user"].id == ali.id
    assert "ticket" not in at.query_params

    again = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
    again.query_params["ticket"] = "used-or-unknown"
    again.run()
    assert "user" not in again.session_state


def test_app_py_as_page_script_runs_the_ui(env, pages):
    """Hosts without st.App discovery (Posit Connect Cloud) execute app.py as the page script."""
    pages("analysis", env.user("ali", Role.ANALYST))  # applies the fixture's patches
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert any(b.label == "ورود" for b in at.button)  # the login form of streamlit_app.py


def test_script_cookie_js():
    """Posit workaround (D-15.9): the cookie string is one JS literal, scoped and expiring."""
    from ui.components import script_cookie_js

    js = script_cookie_js("fda_session", "abc.def-_", 3600)
    assert js.startswith('document.cookie = "fda_session=abc.def-_; Path=/; Max-Age=3600; SameSite=Lax"')
    assert '"; Secure"' in js and "</" not in script_cookie_js("x", "</script>", 0)
