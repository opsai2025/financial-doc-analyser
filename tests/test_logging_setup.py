"""The log stays meaningful: Windows connection-close noise is dropped, real errors are kept."""

import logging

from core.logging_setup import _ClosedConnectionNoise


def _record(message: str, exc: BaseException | None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc else None
    return logging.LogRecord("asyncio", logging.ERROR, __file__, 1, message, None, exc_info)


def test_closed_connection_noise_is_dropped():
    noise = _record("Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
                    ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"))
    assert _ClosedConnectionNoise().filter(noise) is False


def test_other_asyncio_errors_are_kept():
    keep = _ClosedConnectionNoise()
    assert keep.filter(_record("Exception in callback something_else()", ConnectionResetError())) is True
    assert keep.filter(_record("Exception in callback _call_connection_lost()", OSError("disk"))) is True
    assert keep.filter(_record("Task was destroyed but it is pending!", None)) is True
