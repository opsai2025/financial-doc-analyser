"""Entry point — run with `streamlit run app.py`.

Wraps the Streamlit UI script (`streamlit_app.py`) in an ASGI app so that the login flow can
set an HttpOnly session cookie through the `/auth/*` routes.

Some hosts (Posit Connect Cloud) start Streamlit without `streamlit run`'s `st.App` discovery and
execute this file as the page script instead. Then it runs the UI script itself; without the cookie
routes, login lasts for the browser session only (D-15.5).
"""

import runpy
from pathlib import Path

import streamlit as st
from starlette.routing import Route
from streamlit.runtime.scriptrunner import get_script_run_ctx

from core.config import ConfigError, get_settings
from core.logging_setup import configure_logging
from services.auth_routes import logout_route, ping_route, session_route

UI_SCRIPT = Path(__file__).with_name("streamlit_app.py")

if get_script_run_ctx() is not None:  # executed as the page script: no st.App, no cookie routes
    runpy.run_path(str(UI_SCRIPT), run_name="__main__")
else:  # imported by `streamlit run` at server start
    try:  # before the first request, so the cookie routes log to the file too
        configure_logging(get_settings().log_level)
    except ConfigError:
        configure_logging("INFO")  # the UI reports the configuration problem

    app = st.App(
        UI_SCRIPT.name,
        routes=[
            Route("/auth/session", session_route),
            Route("/auth/logout", logout_route),
            Route("/auth/ping", ping_route),
        ],
    )
