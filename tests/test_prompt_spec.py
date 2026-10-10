"""The frozen prompt/schema/config must equal v1 exactly (D-2.1, D-2.2).

v1 (`legacy/app_v1.py`) was removed in Step 4 (D-0.2); until then a test re-extracted its values and
proved them equal. The fingerprint of those verified values is kept here, outside `core/prompt_spec.py`,
so editing the spec (even together with its FINGERPRINT constant) fails this test.
"""

import hashlib
import json

from core import prompt_spec

V1_FINGERPRINT = "d2a6d8bb71820df8c5754afde43e696a9c7e72c22cebddaef98e5d97dc357c2a"


def _fingerprint() -> str:
    canon = json.dumps(
        {"schema": prompt_spec.RESPONSE_SCHEMA, "prompt": prompt_spec.PROMPT, "system": prompt_spec.SYSTEM_INSTRUCTION,
         "mime": prompt_spec.RESPONSE_MIME_TYPE, "temperature": prompt_spec.TEMPERATURE},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def test_fingerprint_equals_v1():
    assert _fingerprint() == V1_FINGERPRINT
    assert prompt_spec.FINGERPRINT == V1_FINGERPRINT


def test_generation_config():
    assert prompt_spec.SYSTEM_INSTRUCTION == "شما تحلیلگر مالی هستید."
    assert prompt_spec.TEMPERATURE == 0.5
    assert prompt_spec.RESPONSE_MIME_TYPE == "application/json"
