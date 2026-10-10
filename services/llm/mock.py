"""Mock provider for development and tests (D-10.7). Never available when APP_ENV=production.

Returns a schema-valid sample answer built from the frozen schema. Behaviour is configured with
the provider's base URL, e.g. ``mock://?delay=1.5&fail_rate=0.3&fail_kind=rate_limit``:

* ``delay``      seconds to sleep per call (default 1.0)
* ``fail_rate``  probability (0..1) that a call fails (default 0)
* ``fail_kind``  rate_limit | transient | invalid_output | auth | permanent (default transient)
* ``fail_names`` comma-separated substrings; files whose name contains one always fail
* ``extra_names``  files whose answer is the JSON followed by extra text (accepted with a warning)
* ``broken_names`` files whose answer is not valid JSON (rejected, kept)
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from urllib.parse import parse_qs, urlparse

from core import prompt_spec
from core.config import get_settings
from services.llm.base import AnalysisOutput, CallConfig, ErrorKind, LlmError, build_output

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _options(base_url: str | None) -> dict:
    query = parse_qs(urlparse(base_url or "").query)
    get = lambda name, default: query.get(name, [default])[0]  # noqa: E731
    return {
        "delay": float(get("delay", "1.0")),
        "fail_rate": float(get("fail_rate", "0")),
        "fail_kind": ErrorKind(get("fail_kind", "transient")),
        "fail_names": [n for n in get("fail_names", "").split(",") if n],
        "extra_names": [n for n in get("extra_names", "").split(",") if n],
        "broken_names": [n for n in get("broken_names", "").split(",") if n],
    }


def _sample(node: dict, key: str, rng: random.Random, index: int = 0):
    kind = node.get("type")
    if "enum" in node:
        return rng.choice(node["enum"])
    if kind == "object":
        return {name: _sample(child, name, rng, index) for name, child in node.get("properties", {}).items()}
    if kind == "array":
        items = node.get("items", {})
        if "enum" in items.get("properties", {}).get("موضوع", {}):  # checklist: one row per topic
            topics = items["properties"]["موضوع"]["enum"]
            return [{**_sample(items, key, rng, i), "موضوع": topic} for i, topic in enumerate(topics)]
        return [_sample(items, key, rng, i) for i in range(rng.randint(1, 3))]
    if kind == "boolean":
        return rng.random() < 0.7
    if kind == "integer":
        return index + 1
    if key in ("شماره_بند", "شماره_صفحه"):
        return str(rng.randint(1, 12)).translate(_FA)
    return f"متن نمونه برای «{key.replace('_', ' ')}» شماره {str(index + 1).translate(_FA)}"


def sample_output(file_name: str) -> dict:
    seed = int(hashlib.sha256(file_name.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    data = _sample(prompt_spec.RESPONSE_SCHEMA, "root", rng)
    summary = data["تحلیل_جامع_گزارش_حسابرسی"]["بخش۱_خلاصه_و_اطلاعات_کلیدی"]
    year_match = re.search(r"(1[34]\d\d)", file_name)
    year = year_match.group(1) if year_match else "1402"
    summary["نام_شرکت"] = "بانک نمونه (سهامی عام)"
    summary["نام_حسابرس"] = "موسسه حسابرسی نمونه"
    summary["دوره_مالی"] = f"سال مالی منتهی به ۲۹ اسفند {year.translate(_FA)}"
    summary["نکات_کلیدی_و_نتیجه_گیری"] = ["یافته نمونه اول", "یافته نمونه دوم", "یافته نمونه سوم"]
    return data


def _guard() -> None:
    if get_settings().is_production:
        raise LlmError(ErrorKind.PERMANENT, "ارائه‌دهنده آزمایشی (mock) در محیط عملیاتی غیرفعال است.")


def analyze_pdf(cfg: CallConfig, pdf: bytes, file_name: str) -> AnalysisOutput:
    _guard()
    opts = _options(cfg.base_url)
    time.sleep(opts["delay"])
    if any(name in file_name for name in opts["fail_names"]) or random.random() < opts["fail_rate"]:
        raise LlmError(opts["fail_kind"], f"خطای شبیه‌سازی‌شده ({opts['fail_kind']})",
                       status=429 if opts["fail_kind"] is ErrorKind.RATE_LIMIT else None, retry_after=1)
    text = json.dumps(sample_output(file_name), ensure_ascii=False)
    if any(name in file_name for name in opts["extra_names"]):
        text += "\n\nتوضیح تکمیلی مدل پس از JSON."
    if any(name in file_name for name in opts["broken_names"]):
        text = text[: len(text) // 2]  # cut off mid-way
    return build_output(text, len(pdf) // 40 or 1, 6000, cfg.send_temperature)


def test_connection(cfg: CallConfig) -> str:
    _guard()
    return "OK"
