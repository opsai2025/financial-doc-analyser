"""Forms shared by several pages."""

from __future__ import annotations

import streamlit as st

from services.auth import CurrentUser, change_password
from ui.session import start_browser_session


def password_change_form(user: CurrentUser, *, form_key: str) -> None:
    with st.form(form_key, border=True, clear_on_submit=False):
        current = st.text_input("رمز عبور فعلی", type="password", autocomplete="current-password")
        new = st.text_input("رمز عبور جدید", type="password", autocomplete="new-password")
        repeat = st.text_input("تکرار رمز عبور جدید", type="password", autocomplete="new-password")
        submitted = st.form_submit_button("ذخیره رمز جدید", icon=":material/save:", type="primary")
    if not submitted:
        return
    if new != repeat:
        st.error("رمز جدید و تکرار آن یکسان نیستند.", icon=":material/error:")
        return
    updated, errors = change_password(user.id, current, new)
    if errors:
        st.error("\n".join(f"- {e}" for e in errors), icon=":material/error:")
        return
    assert updated is not None
    st.toast("رمز عبور با موفقیت تغییر کرد.", icon=":material/check_circle:")
    start_browser_session(updated)  # other sessions were revoked; refresh this browser's cookie
    st.rerun()
