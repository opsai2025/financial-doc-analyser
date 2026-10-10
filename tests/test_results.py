import gzip
import json

from sqlalchemy import select

from core.db import session_scope
from core.models import SHEET_TABLES, AnalysisResult
from services import results
from services.llm import mock


def sample():
    return mock.sample_output("1399.pdf")


def test_extract_year_handles_persian_and_arabic_digits():
    assert results.extract_year("سال مالی منتهی به ۲۹ اسفند ۱۳۹۸") == 1398
    assert results.extract_year("سال مالی منتهی به 29 اسفند 1401") == 1401
    assert results.extract_year("سال ١٤٠٠") == 1400
    assert results.extract_year("نامشخص") is None and results.extract_year(None) is None


def test_flatten_follows_v1_rules():
    data = sample()
    part2 = data["تحلیل_جامع_گزارش_حسابرسی"]["بخش۲_تجزیه_تحلیل_گزارش"]
    part2["بند_تاکید_بر_مطالب_خاص"]["موضوعیت_دارد"] = True
    part2["گزارش_رعایت_الزامات_قانونی"] = {"موضوعیت_دارد": False}
    flat = results.flatten(data)
    assert flat["summary"][0]["year"] == 1399
    emphasis = flat["emphasis"]
    assert emphasis and all(r["موضوعیت_دارد"] is True and "ارجاع" not in r and "شماره_بند" in r for r in emphasis)
    assert flat["legal"] == [{"موضوعیت_دارد": False, "year": 1399}]
    assert len(flat["checklist"]) > 40 and "شماره_صفحه" in flat["checklist"][0]


def test_prepare_normalizes_text_and_keeps_extras():
    data = sample()
    summary = data["تحلیل_جامع_گزارش_حسابرسی"]["بخش۱_خلاصه_و_اطلاعات_کلیدی"]
    summary["نام_شرکت"] = "بانك ملي  ايران"
    summary["فیلد_اضافه"] = "x"
    prepared = results.prepare(data)
    row = prepared.rows["summary"][0]
    assert row["company_name"] == "بانک ملی ایران"
    assert row["key_points"] == "یافته نمونه اول, یافته نمونه دوم, یافته نمونه سوم"
    assert row["extra"] == {"فیلد_اضافه": "x"}
    assert row["year"] == 1399
    assert prepared.size_bytes > len(prepared.raw_json_gz)
    assert json.loads(gzip.decompress(prepared.raw_json_gz)) == data  # raw JSON untouched (D-7.2)


def test_bool_and_int_stored_as_text():
    prepared = results.prepare(sample())
    checklist = prepared.rows["checklist"][0]
    assert checklist["in_report"] in ("True", "False")
    basis = prepared.rows["basis"]
    assert all(isinstance(r.get("item_no"), (str, type(None))) for r in basis)


def test_save_and_load(db):
    from core.models import User

    with session_scope() as s:
        s.add(User(id=1, username="u", display_name="u", password_hash="x", role="analyst"))
    data = sample()
    prepared = results.prepare(data)
    with session_scope() as s:
        results.save_result(s, request_id="R-1", document_id="d" * 64, user_id=1, provider_name="p", model_name="m",
                            api_type="mock", temperature_applied=True, input_tokens=1, output_tokens=2, prepared=prepared)
    with session_scope() as s:
        assert results.load_json(s, "R-1", "d" * 64) == data
        assert s.get(AnalysisResult, ("R-1", "d" * 64)).size_bytes == prepared.size_bytes
        table = SHEET_TABLES["checklist"]
        assert len(s.execute(select(table).where(table.c.request_id == "R-1")).all()) == len(prepared.rows["checklist"])
        assert results.summary_row(s, "R-1", "d" * 64)["year"] == 1399
        results.delete_result(s, "R-1", "d" * 64)
    with session_scope() as s:
        assert results.load_json(s, "R-1", "d" * 64) is None


def test_irregular_items_are_stored_not_fatal(db):
    """Owner's case (R-20261009-WXY5): one checklist item nested under another key, one missing a field.

    Every row gets every column (NULL where missing), the odd shape stays in `extra`, and the save works.
    """
    from core.models import User

    with session_scope() as s:
        s.add(User(id=1, username="u", display_name="u", password_hash="x", role="analyst"))
    data = sample()
    checklist = data[results.ROOT][results.PART3]
    checklist[1] = {"آیتم": checklist[1]}  # nested one level deeper than the schema says
    del checklist[2]["جزئیات"]  # a field left out
    prepared = results.prepare(data)
    assert prepared.unexpected == ("checklist[1]: ['آیتم']",)
    with session_scope() as s:
        results.save_result(s, request_id="R-2", document_id="e" * 64, user_id=1, provider_name="p", model_name="m",
                            api_type="mock", temperature_applied=True, input_tokens=1, output_tokens=2, prepared=prepared)
    table = SHEET_TABLES["checklist"]
    with session_scope() as s:
        rows = s.execute(select(table).where(table.c.request_id == "R-2").order_by(table.c.row_no)).all()
    assert len(rows) == len(checklist)
    assert rows[1].topic is None and "آیتم" in rows[1].extra and rows[1].year == 1399
    assert rows[2].details is None and rows[2].topic


def test_no_length_limit_on_variable_text_columns():
    """Owner rule: model output is not deterministic in size, so nothing it (or a user) produces is capped."""
    from sqlalchemy import JSON, Integer, LargeBinary, Text

    from core.models import ApiKey, RequestDocument, UserDocument

    keys = {"request_id", "document_id", "row_no"}
    for table in SHEET_TABLES.values():
        for column in table.columns:
            if column.name not in keys:
                assert isinstance(column.type, (Text, Integer, JSON)), (table.name, column.name, column.type)
    assert isinstance(AnalysisResult.__table__.c.raw_json_gz.type, LargeBinary)
    for column in (RequestDocument.__table__.c.file_name, RequestDocument.__table__.c.reason,
                   UserDocument.__table__.c.file_name, ApiKey.__table__.c.last_error):
        assert isinstance(column.type, Text), column


def test_very_long_texts_are_stored_whole(db):
    from core.models import User
    from core.text_normalize import normalize_text

    with session_scope() as s:
        s.add(User(id=1, username="u", display_name="u", password_hash="x", role="analyst"))
    data = sample()
    long_text = "بند مشروط درباره کفایت سرمایه، " * 8_000  # ≈250,000 characters
    data[results.ROOT][results.PART3][0]["جزئیات"] = long_text
    data[results.ROOT][results.PART3][0]["مورد_پیش‌بینی‌نشده"] = {"متن": long_text}
    prepared = results.prepare(data)
    with session_scope() as s:
        results.save_result(s, request_id="R-3", document_id="f" * 64, user_id=1, provider_name="p", model_name="m",
                            api_type="mock", temperature_applied=True, input_tokens=1, output_tokens=2, prepared=prepared)
    table = SHEET_TABLES["checklist"]
    with session_scope() as s:
        row = s.execute(select(table).where(table.c.request_id == "R-3", table.c.row_no == 0)).one()
        assert row.details == normalize_text(long_text).strip() and len(row.details) > 240_000
        assert row.extra["مورد_پیش‌بینی‌نشده"]["متن"] == long_text  # unexpected keys kept verbatim
        assert results.load_json(s, "R-3", "f" * 64) == data  # the model's whole answer, unchanged
