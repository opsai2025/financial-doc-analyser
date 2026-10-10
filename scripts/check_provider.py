"""Run ONE real analysis with the TEST_* provider settings in .env and validate the output (D-10.14).

usage:  python scripts/check_provider.py [path/to/report.pdf]

Prints duration, token usage and any schema violations; saves the raw JSON to data/check_provider_<file>.json.
Never prints the API key. Costs one real API call.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from core import prompt_spec  # noqa: E402
from services.llm import CallConfig, LlmError, adapter_for  # noqa: E402
from services.results import flatten  # noqa: E402


def violations(node: dict, value, path: str = "$") -> list[str]:
    """Minimal JSON-schema check (type, enum, required, properties, items)."""
    problems = []
    kind = node.get("type")
    expected = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "number": (int, float)}
    if kind in expected and not isinstance(value, expected[kind]):
        return [f"{path}: expected {kind}, got {type(value).__name__}"]
    if "enum" in node and value not in node["enum"]:
        problems.append(f"{path}: value {value!r} not in enum")
    if kind == "object":
        for name in node.get("required", []):
            if name not in value:
                problems.append(f"{path}.{name}: missing (required)")
        for name, child in node.get("properties", {}).items():
            if name in value:
                problems += violations(child, value[name], f"{path}.{name}")
    if kind == "array":
        for i, item in enumerate(value):
            problems += violations(node.get("items", {}), item, f"{path}[{i}]")
    return problems


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_type = os.getenv("TEST_PROVIDER_TYPE", "").strip().lower().replace("-", "_").replace(" ", "_")
    model = os.getenv("TEST_MODEL", "").strip()
    key = os.getenv("TEST_API_KEY", "").strip()
    if not (api_type and model and key):
        print("Set TEST_PROVIDER_TYPE, TEST_MODEL and TEST_API_KEY in .env first.")
        return 2
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "1398.pdf"
    cfg = CallConfig(api_type=api_type, model=model, api_key=key, base_url=os.getenv("TEST_BASE_URL", "").strip() or None,
                     timeout_s=600, send_temperature=os.getenv("TEST_SEND_TEMPERATURE", "1") != "0")
    print(f"provider={api_type} model={model} base_url={cfg.base_url or '(default)'} file={pdf_path.name}")
    start = time.monotonic()
    try:
        output = adapter_for(api_type).analyze_pdf(cfg, pdf_path.read_bytes(), pdf_path.name)
    except LlmError as exc:
        print(f"FAILED after {time.monotonic() - start:.1f}s: kind={exc.kind} status={exc.status} {exc}")
        if exc.raw_text is not None:
            raw_file = ROOT / "data" / f"check_provider_{pdf_path.stem}_rejected.txt"
            raw_file.parent.mkdir(exist_ok=True)
            raw_file.write_text(exc.raw_text, encoding="utf-8")
            print(f"the model's answer ({len(exc.raw_text)} characters) saved to {raw_file.relative_to(ROOT)}")
        return 1
    elapsed = time.monotonic() - start
    out_file = ROOT / "data" / f"check_provider_{pdf_path.stem}.json"
    out_file.parent.mkdir(exist_ok=True)
    out_file.write_text(json.dumps(output.data, ensure_ascii=False, indent=2), encoding="utf-8")
    problems = violations(prompt_spec.RESPONSE_SCHEMA, output.data)
    sheets = {k: len(v) for k, v in flatten(output.data).items()}
    print(f"OK in {elapsed:.1f}s  tokens in/out={output.input_tokens}/{output.output_tokens}  "
          f"temperature_applied={output.temperature_applied}")
    for warning in output.warnings:
        print(f"WARNING {warning['code']}: json_chars={warning.get('json_chars')} extra_chars={warning.get('extra_chars')}")
    print(f"sheet rows: {sheets}")
    print(f"schema violations: {len(problems)}")
    for problem in problems[:20]:
        print("  -", problem)
    print(f"raw JSON saved to {out_file.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
