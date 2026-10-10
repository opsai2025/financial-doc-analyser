"""Persian text normalization (D-7).

* ``normalize_text`` — Level A: meaning-preserving character fixes for stored/displayed text.
* ``comparison_key`` — Level B: aggressive folding used only for matching (never displayed).
"""

from __future__ import annotations

import re
import unicodedata

ZWNJ = "‌"

# Level A.1 — Arabic letter variants -> Persian
_LETTER_MAP = {
    "ي": "ی",  # ي ARABIC LETTER YEH -> ی
    "ى": "ی",  # ى ALEF MAKSURA -> ی
    "ك": "ک",  # ك ARABIC LETTER KAF -> ک
}
# Level A.3 — Arabic-Indic digits -> Persian digits (Latin digits untouched)
_DIGIT_MAP = {chr(0x0660 + i): chr(0x06F0 + i) for i in range(10)}
# Level A.4/A.5 — characters removed outright
_REMOVE = {
    "ـ",  # tatweel / kashida
    "​",  # zero width space
    "﻿",  # BOM / zero width no-break space
    "‎", "‏",  # LRM, RLM
    *(chr(c) for c in range(0x202A, 0x202F)),  # LRE, RLE, PDF, LRO, RLO
    *(chr(c) for c in range(0x2066, 0x206A)),  # LRI, RLI, FSI, PDI
}
# Level A.6 — unusual spaces -> regular space
_SPACES = {"\t", " ", " ", " ", "　", *(chr(c) for c in range(0x2000, 0x200B))}

_TRANSLATE_A = str.maketrans({**_LETTER_MAP, **_DIGIT_MAP, **{c: None for c in _REMOVE}, **{c: " " for c in _SPACES}})


def _is_presentation_form(ch: str) -> bool:
    cp = ord(ch)
    return 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF


def _expand_presentation_forms(text: str) -> str:
    # Level A.2 — NFKC only on presentation-form characters (e.g. ﻻ -> لا, ﷼ -> ریال)
    if not any(_is_presentation_form(ch) for ch in text):
        return text
    return "".join(unicodedata.normalize("NFKC", ch) if _is_presentation_form(ch) else ch for ch in text)


_MULTI_ZWNJ = re.compile(f"{ZWNJ}{{2,}}")
_ZWNJ_NEAR_SPACE = re.compile(f"(?: +{ZWNJ}+ *| *{ZWNJ}+ +)")
_MULTI_SPACE = re.compile(r" {2,}")
_TRAILING_LINE_SPACE = re.compile(r" *\n *")


def normalize_text(value: object) -> object:
    """Level A normalization. Non-string values are returned unchanged."""
    if not isinstance(value, str):
        return value
    text = _expand_presentation_forms(value).translate(_TRANSLATE_A)
    text = _MULTI_ZWNJ.sub(ZWNJ, text)
    text = _ZWNJ_NEAR_SPACE.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _TRAILING_LINE_SPACE.sub("\n", text)
    return text.strip(" \n" + ZWNJ)


# Level B
_DIACRITICS = re.compile("[ً-ْٰٕٔ]")
_LETTER_FOLD_B = str.maketrans({"ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و"})
_DIGITS_TO_LATIN = str.maketrans({**{chr(0x06F0 + i): str(i) for i in range(10)}, **{chr(0x0660 + i): str(i) for i in range(10)}})


def comparison_key(value: object) -> str:
    """Level B key for matching (company grouping, enum matching, cache lookups)."""
    if value is None:
        return ""
    text = normalize_text(str(value))
    assert isinstance(text, str)
    text = _DIACRITICS.sub("", text).translate(_LETTER_FOLD_B).translate(_DIGITS_TO_LATIN)
    text = "".join(" " if ch == ZWNJ or unicodedata.category(ch)[0] in "PSZC" else ch for ch in text)
    return _MULTI_SPACE.sub(" ", text).strip().lower()
