"""Process logging: console plus a small rotating file in data/logs (local only, never in Supabase)."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import PROJECT_ROOT

# LOG_FILE in the environment moves it (e.g. a scratch test server must not write into data/logs).
LOG_FILE = Path(os.getenv("LOG_FILE") or PROJECT_ROOT / "data" / "logs" / "app.log")
_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"


class _ClosedConnectionNoise(logging.Filter):
    """Drop asyncio's Windows-only report of a browser dropping its connection.

    When a page navigates away (e.g. the logout redirect), the Proactor event loop logs a
    ConnectionResetError (WinError 10054) from `_call_connection_lost`. Nothing failed, so it
    must not look like an error in the log. Every other asyncio error is kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, ConnectionResetError) and "_call_connection_lost" in record.getMessage())


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if getattr(root, "_fda_configured", False):
        return
    formatter = logging.Formatter(_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # read-only deployments: console only
        root.warning("Log file %s is not writable; logging to the console only", LOG_FILE)
    logging.getLogger("asyncio").addFilter(_ClosedConnectionNoise())
    for noisy in ("httpx", "httpcore", "google_genai", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
    root._fda_configured = True  # type: ignore[attr-defined]
