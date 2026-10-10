"""Provider-neutral interface for the LLM adapters (D-6.2) and error classification (D-10.3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from core.fa import fa_digits


@dataclass(frozen=True)
class CallConfig:
    """Everything an adapter needs for one call. Built from Provider + LlmModel + ApiKey rows."""

    api_type: str
    model: str
    api_key: str
    base_url: str | None = None
    timeout_s: int = 300
    send_temperature: bool = True  # D-2.4
    max_output_tokens: int | None = None


# Warning codes stored with a result (D-11.18)
EXTRA_TEXT_AFTER_JSON = "extra_text_after_json"


@dataclass(frozen=True)
class AnalysisOutput:
    data: dict
    input_tokens: int | None
    output_tokens: int | None
    temperature_applied: bool
    raw_text: str | None = None  # the model's answer, kept only when it differs from the parsed JSON
    warnings: tuple[dict, ...] = ()  # {"code", "message", ...}


class ErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"  # 429: retry, cool the key down
    TRANSIENT = "transient"  # 5xx / overloaded / timeout / network: retry
    INVALID_OUTPUT = "invalid_output"  # empty or non-JSON answer: retry
    AUTH = "auth"  # 401/403: skip this key for the rest of the request
    TOO_LARGE = "too_large"  # 413 / file over the model limit: no retry
    PERMANENT = "permanent"  # 400/404, refusal, truncated output...: no retry


RETRYABLE = {ErrorKind.RATE_LIMIT, ErrorKind.TRANSIENT, ErrorKind.INVALID_OUTPUT}


class LlmError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, status: int | None = None, retry_after: float | None = None,
                 raw_text: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        # The model did answer (paid), but the answer can't be used: kept and counted (D-11.18).
        self.raw_text = raw_text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    @property
    def answered(self) -> bool:
        """The provider produced a response (HTTP status or an answer text): counts towards RPD and usage."""
        return self.status is not None or self.raw_text is not None

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


def kind_for_status(status: int | None) -> ErrorKind:
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 413:
        return ErrorKind.TOO_LARGE
    if status is not None and (status >= 500 or status in (408, 409)):
        return ErrorKind.TRANSIENT
    return ErrorKind.PERMANENT


def parse_json_output(text: str | None) -> tuple[dict, list[dict]]:
    """The model's answer -> (JSON, warnings).

    A complete JSON object followed by extra text is accepted with a warning (D-11.18); the caller
    keeps the whole answer. Anything else that isn't one JSON object with the root key is rejected.
    """
    if not text or not text.strip():
        raise LlmError(ErrorKind.INVALID_OUTPUT, "پاسخ مدل خالی بود.")
    warnings: list[dict] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data":
            raise LlmError(ErrorKind.INVALID_OUTPUT, f"پاسخ مدل JSON معتبر نبود ({exc.msg}).") from exc
        start = len(text) - len(text.lstrip())
        data, end = json.JSONDecoder().raw_decode(text, start)
        extra = len(text[end:].strip())
        warnings.append({
            "code": EXTRA_TEXT_AFTER_JSON,
            "message": f"پس از JSON کامل، {fa_digits(extra)} نویسه متن اضافه در پاسخ مدل بود؛ "
                       "JSON اول پذیرفته شد و پاسخ کامل مدل ذخیره شد.",
            "json_chars": end - start,
            "extra_chars": extra,
        })
    if not isinstance(data, dict) or "تحلیل_جامع_گزارش_حسابرسی" not in data:
        raise LlmError(ErrorKind.INVALID_OUTPUT, "ساختار پاسخ مدل با الگوی خروجی مطابقت ندارد.")
    return data, warnings


def build_output(text: str | None, input_tokens: int | None, output_tokens: int | None,
                 temperature_applied: bool) -> AnalysisOutput:
    """Parse an answer; on failure the answer text and token usage travel with the error."""
    try:
        data, warnings = parse_json_output(text)
    except LlmError as exc:
        exc.raw_text, exc.input_tokens, exc.output_tokens = text or "", input_tokens, output_tokens
        raise
    return AnalysisOutput(data=data, input_tokens=input_tokens, output_tokens=output_tokens,
                          temperature_applied=temperature_applied, raw_text=text if warnings else None,
                          warnings=tuple(warnings))


def short_error(exc: BaseException, limit: int = 300) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 1] + "…"
