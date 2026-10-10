"""Adapters send the frozen prompt/schema unchanged; only transport differs (D-2.3..2.5)."""

import base64
import copy
import types

import pytest

from core import prompt_spec
from services.llm import CallConfig, ErrorKind, LlmError, adapter_for
from services.llm import claude, gemini, mock, openai_compat
from services.llm.base import EXTRA_TEXT_AFTER_JSON, build_output, kind_for_status, parse_json_output

PDF = b"%PDF-1.4 test"


def cfg(api_type="gemini", **kw):
    return CallConfig(api_type=api_type, model="m-1", api_key="secret-key", **kw)


class FakeClient:
    """SDK client stand-in: usable as a context manager and refuses calls once closed."""

    def __init__(self, **resources):
        self.closed = False
        for name, resource in resources.items():
            resource.client = self
            setattr(self, name, resource)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def test_gemini_request_matches_v1(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            assert not self.client.closed  # D-11.4: client stays open for the whole call
            captured.update(kwargs)
            return types.SimpleNamespace(text='{"تحلیل_جامع_گزارش_حسابرسی": {}}',
                                         usage_metadata=types.SimpleNamespace(prompt_token_count=10, candidates_token_count=20))

    clients = []
    monkeypatch.setattr(gemini, "_client", lambda c: clients.append(FakeClient(models=FakeModels())) or clients[-1])
    out = gemini.analyze_pdf(cfg(base_url="https://api.avalai.ir"), PDF, "a.pdf")
    assert clients[0].closed  # and is closed afterwards
    # v1: contents=[Part.from_bytes(pdf), prompt]; config = system_instruction, mime, schema, temperature 0.5
    assert captured["model"] == "m-1"
    assert captured["contents"][1] == prompt_spec.PROMPT
    assert captured["contents"][0].inline_data.data == PDF
    assert captured["config"] == {
        "system_instruction": prompt_spec.SYSTEM_INSTRUCTION,
        "response_mime_type": "application/json",
        "response_schema": prompt_spec.RESPONSE_SCHEMA,
        "temperature": 0.5,
    }
    assert (out.input_tokens, out.output_tokens, out.temperature_applied) == (10, 20, True)


def test_temperature_switch():
    assert "temperature" not in gemini.build_config(cfg(send_temperature=False))
    assert "temperature" not in openai_compat.build_request(cfg("openai_compatible", send_temperature=False), PDF, "a.pdf")
    assert "temperature" not in claude.build_request(cfg("anthropic", send_temperature=False), PDF)
    assert claude.build_request(cfg("anthropic"), PDF)["temperature"] == 0.5


def test_openai_request():
    request = openai_compat.build_request(cfg("openai_compatible"), PDF, "گزارش.pdf")
    system, user = request["messages"]
    assert system == {"role": "system", "content": prompt_spec.SYSTEM_INSTRUCTION}
    file_part, text_part = user["content"]
    assert file_part["file"]["filename"] == "گزارش.pdf"
    assert base64.b64decode(file_part["file"]["file_data"].split(",", 1)[1]) == PDF
    assert text_part == {"type": "text", "text": prompt_spec.PROMPT}
    schema = request["response_format"]["json_schema"]
    assert schema["schema"] is prompt_spec.RESPONSE_SCHEMA and schema["strict"] is False


def _strip_additional(node):
    if isinstance(node, dict):
        return {k: _strip_additional(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [_strip_additional(v) for v in node]
    return node


def test_claude_schema_only_closes_objects():
    original = copy.deepcopy(prompt_spec.RESPONSE_SCHEMA)
    closed = claude.claude_schema()
    assert _strip_additional(closed) == original
    assert prompt_spec.RESPONSE_SCHEMA == original  # frozen schema untouched

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    assert all(o.get("additionalProperties") is False for o in objects(closed))
    request = claude.build_request(cfg("anthropic"), PDF)
    doc, text = request["messages"][0]["content"]
    assert doc["type"] == "document" and base64.b64decode(doc["source"]["data"]) == PDF
    assert text["text"] == prompt_spec.PROMPT and request["system"] == prompt_spec.SYSTEM_INSTRUCTION


@pytest.mark.parametrize("status,kind", [(429, ErrorKind.RATE_LIMIT), (401, ErrorKind.AUTH), (403, ErrorKind.AUTH),
                                         (413, ErrorKind.TOO_LARGE), (500, ErrorKind.TRANSIENT), (503, ErrorKind.TRANSIENT),
                                         (400, ErrorKind.PERMANENT), (404, ErrorKind.PERMANENT)])
def test_status_classification(status, kind):
    assert kind_for_status(status) is kind


def test_parse_json_output_errors():
    for bad in ("", "  ", "not json", "[1,2]", '{"other": 1}', '{"تحلیل_جامع_گزارش_حسابرسی": {"x": '):
        with pytest.raises(LlmError) as exc:
            parse_json_output(bad)
        assert exc.value.kind is ErrorKind.INVALID_OUTPUT and exc.value.retryable
    assert parse_json_output('{"تحلیل_جامع_گزارش_حسابرسی": {"x": 1}}') == ({"تحلیل_جامع_گزارش_حسابرسی": {"x": 1}}, [])


def test_extra_text_after_json_is_accepted_with_warning():
    """D-11.18: owner's case "Extra data: line 1 column 21910" -> first JSON used, whole answer kept."""
    first = '{"تحلیل_جامع_گزارش_حسابرسی": {"x": 1}}'
    rest = '{"تحلیل_جامع_گزارش_حسابرسی": {"x": 2}} توضیح'
    answer = f"  {first}\n{rest}"
    out = build_output(answer, 100, 50, True)
    assert out.data == {"تحلیل_جامع_گزارش_حسابرسی": {"x": 1}}
    assert out.raw_text == answer  # nothing lost
    (warning,) = out.warnings
    assert warning["code"] == EXTRA_TEXT_AFTER_JSON
    assert (warning["json_chars"], warning["extra_chars"]) == (len(first), len(rest))
    clean = build_output('{"تحلیل_جامع_گزارش_حسابرسی": {}}', 1, 2, True)
    assert clean.raw_text is None and clean.warnings == ()


def test_unusable_answer_travels_with_the_error():
    with pytest.raises(LlmError) as exc:
        build_output('{"تحلیل_جامع_گزارش_حسابرسی": {"x"', 90_000, 12_000, True)
    err = exc.value
    assert err.raw_text == '{"تحلیل_جامع_گزارش_حسابرسی": {"x"' and (err.input_tokens, err.output_tokens) == (90_000, 12_000)
    assert err.answered  # counts towards RPD and usage
    assert not LlmError(ErrorKind.TRANSIENT, "timeout").answered


def test_mock_output_is_schema_shaped():
    out = mock.analyze_pdf(cfg("mock", base_url="mock://?delay=0"), PDF, "1398.pdf")
    summary = out.data["تحلیل_جامع_گزارش_حسابرسی"]["بخش۱_خلاصه_و_اطلاعات_کلیدی"]
    assert "۱۳۹۸" in summary["دوره_مالی"]
    checklist = out.data["تحلیل_جامع_گزارش_حسابرسی"]["بخش۳_چک_لیست_موضوعی"]
    topics = prompt_spec.RESPONSE_SCHEMA["properties"]["تحلیل_جامع_گزارش_حسابرسی"]["properties"]["بخش۳_چک_لیست_موضوعی"]
    assert len(checklist) == len(topics["items"]["properties"]["موضوع"]["enum"])
    with pytest.raises(LlmError) as exc:
        mock.analyze_pdf(cfg("mock", base_url="mock://?delay=0&fail_names=bad&fail_kind=auth"), PDF, "bad.pdf")
    assert exc.value.kind is ErrorKind.AUTH


def test_adapter_registry():
    assert adapter_for("anthropic") is claude
    with pytest.raises(LlmError):
        adapter_for("unknown")
