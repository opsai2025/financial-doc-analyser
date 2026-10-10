"""Views shown before the regular pages: login and forced password change.

They are plain functions (not st.Page objects) because they replace whatever page the URL selects.
"""

from __future__ import annotations

import streamlit as st

from core.fa import jalali_datetime
from services.auth import CurrentUser, LoginError, authenticate
from services.auth_routes import LOGIN_FAILED_PARAM
from ui.components import help_box
from ui.forms import password_change_form
from ui.session import logout, start_browser_session


HANDOFF_ERRORS = {
    "expired": "زمان تکمیل ورود به پایان رسید؛ دوباره وارد شوید.",
    "revoked": "حساب شما در این فاصله غیرفعال شد یا نشست آن لغو شد؛ با مدیر سامانه تماس بگیرید.",
}


def _login_notices() -> None:
    """Explain why the login form is shown again, so a failed sign-in is never silent (D-11.3)."""
    failed = st.query_params.get(LOGIN_FAILED_PARAM)
    if failed:
        del st.query_params[LOGIN_FAILED_PARAM]
        st.session_state.pop("session_ended", None)
        st.error(HANDOFF_ERRORS.get(failed, "ورود کامل نشد. دوباره تلاش کنید؛ اگر تکرار شد به مدیر سامانه اطلاع دهید."),
                 icon=":material/error:")
    elif st.session_state.pop("session_ended", None):
        st.info("نشست قبلی شما پایان یافته است؛ دوباره وارد شوید.", icon=":material/schedule:")


def login_view() -> None:
    _, center, _ = st.columns([1, 1.3, 1])
    with center:
        st.subheader(":material/login: ورود به سامانه", anchor=False)
        _login_notices()
        with st.form("login_form", border=True):
            with st.container(key="fda_ltr_username"):
                username = st.text_input("نام کاربری", autocomplete="username", max_chars=32)
            password = st.text_input("رمز عبور", type="password", autocomplete="current-password", max_chars=128)
            submitted = st.form_submit_button("ورود", icon=":material/login:", type="primary", width="stretch")

        if submitted:
            if not username or not password:
                st.warning("نام کاربری و رمز عبور را وارد کنید.", icon=":material/warning:")
            else:
                with st.spinner("در حال بررسی…"):
                    result = authenticate(username, password)
                if result.user is not None:
                    start_browser_session(result.user)
                    st.rerun()
                elif result.error is LoginError.LOCKED:
                    st.error(
                        "به دلیل تلاش‌های ناموفق متعدد، حساب به‌طور موقت قفل شده است. "
                        f"تا {jalali_datetime(result.locked_until)} صبر کنید یا با مدیر سامانه تماس بگیرید.",
                        icon=":material/lock_clock:",
                    )
                elif result.error is LoginError.INACTIVE:
                    st.error("حساب کاربری شما غیرفعال است. با مدیر سامانه تماس بگیرید.", icon=":material/block:")
                elif result.error is LoginError.EXPIRED:
                    st.error("مهلت اعتبار حساب مهمان شما به پایان رسیده است.", icon=":material/event_busy:")
                else:
                    st.error("نام کاربری یا رمز عبور نادرست است.", icon=":material/error:")

        help_box(
            "راهنمای ورود",
            [
                "حساب‌های کاربری فقط توسط مدیر سامانه ایجاد می‌شوند.",
                "در اولین ورود باید رمز عبور موقت خود را تغییر دهید.",
                "پس از ۵ تلاش ناموفق، حساب به مدت ۱۵ دقیقه قفل می‌شود.",
            ],
            variant="blue",
            icon="help",
        )


def change_password_view(user: CurrentUser) -> None:
    _, center, _ = st.columns([1, 1.3, 1])
    with center:
        st.subheader(":material/password: تغییر رمز عبور موقت", anchor=False)
        help_box(
            "پیش از ادامه، رمز عبور جدید تعیین کنید",
            [
                "رمز عبور فعلی شما موقت است و توسط مدیر سامانه تعیین شده است.",
                "رمز جدید باید حداقل ۸ نویسه و شامل حرف و رقم باشد.",
            ],
            variant="green",
            icon="key",
        )
        password_change_form(user, form_key="forced_change")
        if st.button("خروج", icon=":material/logout:", key="danger_logout_forced"):
            logout()
            st.rerun()
