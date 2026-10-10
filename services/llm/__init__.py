"""LLM adapters by API type (D-6.2). Each module exposes analyze_pdf(cfg, pdf, file_name) and test_connection(cfg)."""

from __future__ import annotations

from types import ModuleType

from core.models import ApiType
from services.llm.base import AnalysisOutput, CallConfig, ErrorKind, LlmError


def adapter_for(api_type: str) -> ModuleType:
    if api_type == ApiType.GEMINI:
        from services.llm import gemini as module
    elif api_type == ApiType.OPENAI_COMPATIBLE:
        from services.llm import openai_compat as module
    elif api_type == ApiType.ANTHROPIC:
        from services.llm import claude as module
    elif api_type == ApiType.MOCK:
        from services.llm import mock as module
    else:
        raise LlmError(ErrorKind.PERMANENT, f"نوع API ناشناخته: {api_type}")
    return module


__all__ = ["AnalysisOutput", "CallConfig", "ErrorKind", "LlmError", "adapter_for"]
