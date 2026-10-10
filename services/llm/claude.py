"""Anthropic Claude (Messages API, official SDK).

PDF as a base64 `document` block before the prompt text; JSON via structured outputs
(`output_config.format`). Claude requires `additionalProperties: false` on every object, so a
copy of the frozen schema gets that key added (D-2.5). Streaming avoids HTTP timeouts on long
outputs.
"""

from __future__ import annotations

import base64
import copy
from functools import lru_cache

import anthropic

from core import prompt_spec
from services.llm.base import AnalysisOutput, CallConfig, ErrorKind, LlmError, build_output, kind_for_status, short_error

DEFAULT_MAX_OUTPUT_TOKENS = 64000


def _close_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for value in node.values():
            _close_objects(value)
    elif isinstance(node, list):
        for value in node:
            _close_objects(value)
    return node


@lru_cache(maxsize=1)
def claude_schema() -> dict:
    return _close_objects(copy.deepcopy(prompt_spec.RESPONSE_SCHEMA))


def _client(cfg: CallConfig) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=cfg.timeout_s, max_retries=0)


def build_request(cfg: CallConfig, pdf: bytes) -> dict:
    request = {
        "model": cfg.model,
        "max_tokens": cfg.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
        "system": prompt_spec.SYSTEM_INSTRUCTION,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf",
                                   "data": base64.standard_b64encode(pdf).decode("ascii")},
                    },
                    {"type": "text", "text": prompt_spec.PROMPT},
                ],
            }
        ],
        "output_config": {"format": {"type": "json_schema", "schema": claude_schema()}},
    }
    if cfg.send_temperature:
        request["temperature"] = prompt_spec.TEMPERATURE
    return request


def _translate(exc: Exception) -> LlmError:
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return LlmError(ErrorKind.TRANSIENT, short_error(exc))
    if isinstance(exc, anthropic.APIStatusError):
        retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
        status = exc.status_code
        kind = ErrorKind.TRANSIENT if status == 529 else kind_for_status(status)
        return LlmError(kind, short_error(exc), status=status,
                        retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None)
    return LlmError(ErrorKind.TRANSIENT, short_error(exc))


def analyze_pdf(cfg: CallConfig, pdf: bytes, file_name: str) -> AnalysisOutput:
    try:
        with _client(cfg) as client, client.messages.stream(**build_request(cfg, pdf)) as stream:
            message = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    tokens = {"input_tokens": message.usage.input_tokens, "output_tokens": message.usage.output_tokens}
    text = next((block.text for block in message.content if block.type == "text"), None)
    if message.stop_reason == "refusal":
        raise LlmError(ErrorKind.PERMANENT, "مدل از پاسخ‌گویی به این سند خودداری کرد (refusal).",
                       raw_text=text or "", **tokens)
    if message.stop_reason == "max_tokens":
        raise LlmError(ErrorKind.PERMANENT, "پاسخ مدل به دلیل سقف توکن خروجی ناقص ماند؛ سقف خروجی مدل را افزایش دهید.",
                       raw_text=text or "", **tokens)
    return build_output(text, temperature_applied=cfg.send_temperature, **tokens)


def test_connection(cfg: CallConfig) -> str:
    try:
        with _client(cfg) as client:
            message = client.messages.create(
                model=cfg.model, max_tokens=64, messages=[{"role": "user", "content": "Reply with the single word: OK"}]
            )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return next((block.text for block in message.content if block.type == "text"), "").strip()
