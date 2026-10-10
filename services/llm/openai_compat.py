"""OpenAI-compatible Chat Completions (OpenAI, OpenRouter, gateways, local servers).

The PDF goes as a `file` content part; the frozen schema as a non-strict `json_schema`
response format (strict mode would require schema changes, D-2.1).
"""

from __future__ import annotations

import base64

import openai

from core import prompt_spec
from services.llm.base import AnalysisOutput, CallConfig, ErrorKind, LlmError, build_output, kind_for_status, short_error

SCHEMA_NAME = "audit_report_analysis"


def _client(cfg: CallConfig) -> openai.OpenAI:
    return openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=cfg.timeout_s, max_retries=0)


def build_request(cfg: CallConfig, pdf: bytes, file_name: str) -> dict:
    data_url = "data:application/pdf;base64," + base64.b64encode(pdf).decode("ascii")
    request = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": prompt_spec.SYSTEM_INSTRUCTION},
            {
                "role": "user",
                "content": [
                    {"type": "file", "file": {"filename": file_name, "file_data": data_url}},
                    {"type": "text", "text": prompt_spec.PROMPT},
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "schema": prompt_spec.RESPONSE_SCHEMA, "strict": False},
        },
    }
    if cfg.send_temperature:
        request["temperature"] = prompt_spec.TEMPERATURE
    if cfg.max_output_tokens:
        request["max_completion_tokens"] = cfg.max_output_tokens
    return request


def _translate(exc: Exception) -> LlmError:
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return LlmError(ErrorKind.TRANSIENT, short_error(exc))
    if isinstance(exc, openai.APIStatusError):
        retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
        return LlmError(
            kind_for_status(exc.status_code),
            short_error(exc),
            status=exc.status_code,
            retry_after=float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None,
        )
    return LlmError(ErrorKind.TRANSIENT, short_error(exc))


def analyze_pdf(cfg: CallConfig, pdf: bytes, file_name: str) -> AnalysisOutput:
    try:
        with _client(cfg) as client:  # closes the connection pool after the call
            response = client.chat.completions.create(**build_request(cfg, pdf, file_name))
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    usage = response.usage
    tokens = {"input_tokens": getattr(usage, "prompt_tokens", None),
              "output_tokens": getattr(usage, "completion_tokens", None)}
    choice = response.choices[0] if response.choices else None
    if choice is None:
        raise LlmError(ErrorKind.INVALID_OUTPUT, "پاسخ مدل خالی بود.", raw_text="", **tokens)
    text = choice.message.content
    if getattr(choice.message, "refusal", None):
        raise LlmError(ErrorKind.PERMANENT, f"مدل از پاسخ‌گویی خودداری کرد: {choice.message.refusal}",
                       raw_text=text or choice.message.refusal, **tokens)
    if choice.finish_reason == "length":
        raise LlmError(ErrorKind.PERMANENT, "پاسخ مدل به دلیل سقف توکن خروجی ناقص ماند؛ سقف خروجی مدل را افزایش دهید.",
                       raw_text=text or "", **tokens)
    return build_output(text, temperature_applied=cfg.send_temperature, **tokens)


def test_connection(cfg: CallConfig) -> str:
    try:
        with _client(cfg) as client:
            response = client.chat.completions.create(
                model=cfg.model, messages=[{"role": "user", "content": "Reply with the single word: OK"}],
                max_completion_tokens=16,
            )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return ((response.choices[0].message.content if response.choices else "") or "").strip()
