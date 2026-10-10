"""ASGI routes that set / clear the HttpOnly session cookie (mounted in app.py)."""

from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from core.config import get_settings
from services.auth import SESSION_COOKIE, redeem_login_ticket

log = logging.getLogger(__name__)

# Routes live at <base>/auth/<name>; "../" brings the browser back to the app root,
# which also works when the app is served under a sub-path.
_APP_ROOT = "../"
# Shown by the login view when the hand-off failed, so a failure is never silent (D-11.3).
LOGIN_FAILED_PARAM = "login_failed"
# Answer of /auth/ping. Without the routes the host serves the app page there (status 200), so the
# browser compares the body before using the other routes (ui.components.redirect).
PING_REPLY = "fda-auth-ok"


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",")[0].strip() == "https"


async def session_route(request: Request) -> RedirectResponse:
    # The database lookup is blocking; keep it off the event loop.
    try:
        token, error = await run_in_threadpool(redeem_login_ticket, request.query_params.get("ticket", ""))
    except Exception:  # noqa: BLE001 - database down etc.: report instead of failing silently
        log.exception("Login ticket could not be redeemed")
        token, error = None, "error"
    if not token:
        log.warning("Login hand-off failed (%s) from %s", error, request.client.host if request.client else "?")
        return RedirectResponse(f"{_APP_ROOT}?{LOGIN_FAILED_PARAM}={error}", status_code=303)
    response = RedirectResponse(_APP_ROOT, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=get_settings().session_hours * 3600,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        path="/",
    )
    return response


async def ping_route(_request: Request) -> PlainTextResponse:
    return PlainTextResponse(PING_REPLY, headers={"Cache-Control": "no-store"})


async def logout_route(request: Request) -> RedirectResponse:
    response = RedirectResponse(_APP_ROOT, status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
