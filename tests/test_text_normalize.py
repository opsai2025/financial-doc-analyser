from core.text_normalize import ZWNJ, comparison_key, normalize_text


def test_arabic_letters_to_persian():
    assert normalize_text("بانك ملي") == "بانک ملی"
    assert normalize_text("مصطفى") == "مصطفی"


def test_arabic_indic_digits_only():
    assert normalize_text("سال ١٣٩٨") == "سال ۱۳۹۸"
    assert normalize_text("AML/CFT 2024") == "AML/CFT 2024"  # Latin digits untouched
    assert normalize_text("۱۴۰۲") == "۱۴۰۲"


def test_presentation_forms():
    assert normalize_text("ﻻ") == "لا"
    assert normalize_text("۱۰۰ ﷼") == "۱۰۰ ریال"
    assert normalize_text("ﺎﺑ") == "اب"


def test_kashida_and_invisible_chars_removed():
    assert normalize_text("بانـــک") == "بانک"
    assert normalize_text("‏متن‎​﻿") == "متن"


def test_zwnj_rules():
    assert normalize_text(f"می{ZWNJ}{ZWNJ}شود") == f"می{ZWNJ}شود"
    assert normalize_text(f"کتاب {ZWNJ}ها") == "کتاب ها"
    assert normalize_text(f"{ZWNJ}متن{ZWNJ}") == "متن"
    assert normalize_text(f"گزارش{ZWNJ}ها") == f"گزارش{ZWNJ}ها"


def test_spaces():
    assert normalize_text("  الف  ب\t ج  ") == "الف ب ج"
    assert normalize_text("خط اول  \n  خط دوم") == "خط اول\nخط دوم"


def test_no_level_c_rewriting():
    assert normalize_text("می شود") == "می شود"
    assert normalize_text('"نقل"') == '"نقل"'
    assert normalize_text("مؤسسه") == "مؤسسه"


def test_non_strings_unchanged():
    assert normalize_text(5) == 5
    assert normalize_text(None) is None
    assert normalize_text(True) is True


def test_comparison_key_folds_variants():
    a = comparison_key("بانك ملّت (سهامی عام)")
    b = comparison_key("بانک  ملت سهامی‌عام")
    assert a == b == "بانک ملت سهامی عام"
    assert comparison_key("مؤسسة") == comparison_key("موسسه")
    assert comparison_key("سال ۱۳۹۸") == comparison_key("سال 1398") == comparison_key("سال ١٣٩٨")
    assert comparison_key("AML") == "aml"
    assert comparison_key(None) == ""
