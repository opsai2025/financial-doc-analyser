"""Per-browser-session login state (st.session_state) on top of services.auth."""

from __future__ import annotations

import logging
import time

import streamlit as st

from services.auth import (
    SESSION_COOKIE,
    CurrentUser,
    create_login_ticket,
    load_active_user,
    redeem_login_ticket,
    user_from_session_token,
)

USER_REFRESH_SECONDS = 60
log = logging.getLogger(__name__)


def start_browser_session(user: CurrentUser) -> None:
    """Store the user and let the browser receive the HttpOnly cookie via /auth/session.

    If the host does not serve the /auth/* routes, the browser comes back to the app root with the
    ticket instead (see _session_only_login).
    """
    st.session_state.user = user
    st.session_state.pop("logged_out", None)
    ticket = create_login_ticket(user)
    st.session_state.pending_redirect = (f"auth/session?ticket={ticket}", f"./?ticket={ticket}")


def logout() -> None:
    user = st.session_state.get("user")
    if user is not None:
        log.info("Logout: user=%s", user.username)
    st.session_state.clear()
    st.session_state.logged_out = True  # ignore the old cookie until the browser reloads
    st.session_state.pending_redirect = ("auth/logout", "./")


def resolve_user() -> CurrentUser | None:
    """Current user from session state (re-validated every minute) or from the session cookie."""
    user: CurrentUser | None = st.session_state.get("user")
    if user is not None:
        if time.monotonic() - user.loaded_at > USER_REFRESH_SECONDS:
            user = load_active_user(user.id, user.session_version)
            st.session_state.user = user
            if user is None:
                st.session_state.session_ended = True
        return user
    if st.session_state.get("logged_out"):
        return None
    if ticket := st.query_params.get("ticket"):
        return _session_only_login(ticket)
    token = st.context.cookies.get(SESSION_COOKIE)
    user = user_from_session_token(token)
    if user is not None:
        st.session_state.user = user
    elif token:
        # A cookie that no longer opens a session (expired, revoked, account disabled).
        st.session_state.session_ended = True
    return user


def _session_only_login(ticket: str) -> CurrentUser | None:
    """Fallback when the host does not serve the /auth/* routes (app not launched through st.App).

    The browser then lands back in the app with the unredeemed ticket in the URL. Redeeming it here
    logs the user in for this browser session only: a refresh asks for the password again.
    """
    del st.query_params["ticket"]
    token, error = redeem_login_ticket(ticket)
    user = user_from_session_token(token)
    if user is None:
        log.warning("Session-only login failed (%s)", error)
        return None
    log.warning("Cookie routes are not served; user=%s is logged in for this browser session only", user.username)
    st.session_state.user = user
    return user


def require_user() -> CurrentUser:
    """For page scripts: the logged-in user (pages are only registered for logged-in users)."""
    user = st.session_state.get("user")
    if user is None:
        st.stop()
    return user
