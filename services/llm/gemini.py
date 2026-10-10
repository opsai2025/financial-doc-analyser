"""Google Gemini (native API, optional custom base URL such as a gateway).

The request is built exactly like v1 (`legacy/app_v1.py`, removed in Step 4): contents = [PDF part, PROMPT] and
config = system_instruction + response_mime_type + response_schema + temperature (D-2).
"""

from __future__ import annotations

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from core import prompt_spec
from services.llm.base import AnalysisOutput, CallConfig, ErrorKind, LlmError, build_output, kind_for_status, short_error


def _client(cfg: CallConfig) -> genai.Client:
    http_options = types.HttpOptions(timeout=cfg.timeout_s * 1000, base_url=cfg.base_url or None)
    return genai.Client(api_key=cfg.api_key, http_options=http_options)


def build_config(cfg: CallConfig) -> dict:
    config = {
        "system_instruction": prompt_spec.SYSTEM_INSTRUCTION,
        "response_mime_type": prompt_spec.RESPONSE_MIME_TYPE,
        "response_schema": prompt_spec.RESPONSE_SCHEMA,
    }
    if cfg.send_temperature:
        config["temperature"] = prompt_spec.TEMPERATURE
    if cfg.max_output_tokens:
        config["max_output_tokens"] = cfg.max_output_tokens
    return config


def _translate(exc: Exception) -> LlmError:
    if isinstance(exc, genai_errors.APIError):
        return LlmError(kind_for_status(exc.code), short_error(exc), status=exc.code)
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError)):
        return LlmError(ErrorKind.TRANSIENT, short_error(exc))
    return LlmError(ErrorKind.TRANSIENT, short_error(exc))


def analyze_pdf(cfg: CallConfig, pdf: bytes, file_name: str) -> AnalysisOutput:
    try:
        # Keep a reference to the client for the whole call: genai.Client closes its HTTP
        # client when garbage-collected, so `_client(cfg).models...` fails with
        # "Cannot send a request, as the client has been closed" (D-11.4).
        with _client(cfg) as client:
            response = client.models.generate_content(
                model=cfg.model,
                contents=[types.Part.from_bytes(data=pdf, mime_type="application/pdf"), prompt_spec.PROMPT],
                config=build_config(cfg),
            )
    except Exception as exc:  # noqa: BLE001 - every SDK/network error is classified
        raise _translate(exc) from exc
    usage = getattr(response, "usage_metadata", None)
    return build_output(getattr(response, "text", None), getattr(usage, "prompt_token_count", None),
                        getattr(usage, "candidates_token_count", None), cfg.send_temperature)


def test_connection(cfg: CallConfig) -> str:
    try:
        with _client(cfg) as client:
            response = client.models.generate_content(
                model=cfg.model, contents=["Reply with the single word: OK"], config={"max_output_tokens": 16}
            )
    except Exception as exc:  # noqa: BLE001
        raise _translate(exc) from exc
    return (getattr(response, "text", None) or "").strip()
