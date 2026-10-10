"""Streamlit UI script: theme, authentication gate and navigation.

Launched through `app.py` (`streamlit run app.py`), which adds the cookie routes.
"""

from __future__ import annotations

import logging

import streamlit as st

from core.config import ConfigError, get_settings
from core.logging_setup import configure_logging
from core.migrate import upgrade_database
from core.models import Role
from services.auth import ensure_bootstrap_admin
from services.jobs import get_job_manager
from services.providers import purge_expired_guest_providers
from services.users import ROLE_LABELS
from ui.auth_views import change_password_view, login_view
from ui.components import app_header, inject_theme, md_escape, redirect, sidebar_brand
from ui.session import logout, resolve_user

st.set_page_config(
    page_title="تحلیل گزارش‌های حسابرسی",
    page_icon=":material/query_stats:",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_theme()


@st.cache_resource
def bootstrap() -> str | None:
    """One-time process setup: logging, schema, bootstrap admin. Returns an error message or None."""
    configure_logging(get_settings().log_level)
    upgrade_database()
    error = ensure_bootstrap_admin()
    purge_expired_guest_providers()
    get_job_manager()  # starts the engine and marks work left by a previous process as interrupted
    return error


def fatal(message: str) -> None:
    app_header()
    st.error(message, icon=":material/error:")
    st.stop()


try:
    bootstrap_error = bootstrap()
except ConfigError as exc:
    fatal(f"پیکربندی سامانه ناقص است: {exc}")
except Exception:  # database unreachable, etc.
    logging.getLogger(__name__).exception("Startup failed")
    fatal("اتصال به پایگاه داده برقرار نشد. تنظیمات DATABASE_URL را بررسی کنید.")
if bootstrap_error:
    fatal(bootstrap_error)


if pending := st.session_state.pop("pending_redirect", None):
    redirect(*pending)
    st.info("در حال انتقال…", icon=":material/hourglass_top:")
    st.stop()

current_user = resolve_user()

# --- Pages ---------------------------------------------------------------------------------

ALL = frozenset(Role)
PAGES = {
    "analysis": (st.Page("app_pages/analysis.py", title="تحلیل اسناد", icon=":material/upload_file:", url_path="analysis"),
                 {Role.ADMIN, Role.ANALYST, Role.GUEST}),
    "history": (st.Page("app_pages/history.py", title="تاریخچه تحلیل‌ها", icon=":material/history:", url_path="history"),
                {Role.ADMIN, Role.ANALYST, Role.VIEWER}),
    "dashboard": (st.Page("app_pages/dashboard.py", title="داشبورد نتایج", icon=":material/monitoring:", url_path="dashboard"),
                  ALL),
    "users": (st.Page("app_pages/users.py", title="مدیریت کاربران", icon=":material/manage_accounts:", url_path="users"),
              {Role.ADMIN}),
    "settings": (st.Page("app_pages/settings.py", title="تنظیمات سامانه", icon=":material/settings:", url_path="settings"),
                 {Role.ADMIN}),
    "profile": (st.Page("app_pages/profile.py", title="پروفایل من", icon=":material/account_circle:", url_path="profile"),
                ALL),
}
MENU = (
    ("منوی اصلی", ("analysis", "history", "dashboard")),
    ("مدیریت", ("users", "settings")),
    ("حساب کاربری", ("profile",)),
)
DEFAULT_PAGE = {Role.ADMIN: "analysis", Role.ANALYST: "analysis", Role.GUEST: "analysis", Role.VIEWER: "history"}

# Every page is always registered (so unknown/forbidden URLs never trigger Streamlit's English
# "page not found" dialog); access is enforced below before a page runs.
role = current_user.role if current_user else None
default_key = DEFAULT_PAGE.get(role, "analysis")


def home() -> None:
    """Landing page at "/": forwards to the role's start page, so real pages keep stable URLs."""
    st.switch_page(PAGES[default_key][0])


HOME_PAGE = st.Page(home, title="خانه", icon=":material/home:", url_path="home", default=True)
selected = st.navigation([HOME_PAGE, *(page for page, _ in PAGES.values())], position="hidden")

if current_user is None:
    app_header()
    login_view()
    st.stop()
if current_user.must_change_password:
    app_header()
    change_password_view(current_user)
    st.stop()

allowed = [key for key, (_, roles) in PAGES.items() if current_user.role in roles]
with st.sidebar:
    sidebar_brand()
    for section, keys in MENU:
        visible = [k for k in keys if k in allowed]
        if not visible:
            continue
        st.caption(section)
        for key in visible:
            st.page_link(PAGES[key][0], width="stretch")
    st.divider()
    with st.container(border=True):
        st.markdown(f":material/person: **{md_escape(current_user.display_name)}**")
        st.badge(ROLE_LABELS[current_user.role], icon=":material/badge:", color="green")
    if st.button("خروج از سامانه", icon=":material/logout:", key="danger_logout", width="stretch"):
        logout()
        st.rerun()

app_header()
selected_key = next((k for k, (pg, _) in PAGES.items() if pg.url_path == selected.url_path), None)
if selected_key in allowed or selected.url_path == HOME_PAGE.url_path:
    selected.run()
else:
    st.warning("شما به این بخش دسترسی ندارید. از منوی کناری یکی از بخش‌های مجاز را انتخاب کنید.",
               icon=":material/lock:")
