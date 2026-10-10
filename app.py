"""Entry point — run with `streamlit run app.py`.

Wraps the Streamlit UI script (`streamlit_app.py`) in an ASGI app so that the login flow can
set an HttpOnly session cookie through the `/auth/*` routes.
"""

import streamlit as st
from starlette.routing import Route

from core.config import ConfigError, get_settings
from core.logging_setup import configure_logging
from services.auth_routes import logout_route, ping_route, session_route

try:  # before the first request, so the cookie routes log to the file too
    configure_logging(get_settings().log_level)
except ConfigError:
    configure_logging("INFO")  # the UI reports the configuration problem

app = st.App(
    "streamlit_app.py",
    routes=[
        Route("/auth/session", session_route),
        Route("/auth/logout", logout_route),
        Route("/auth/ping", ping_route),
    ],
)
