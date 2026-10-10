"""Model JSON -> v1 sheet rows, persistence of results, reading them back (D-7, D-8, D-10.10)."""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass

from sqlalchemy import delete, insert, select

from core.models import SHEET_TABLES, AnalysisResult, RawOutput
from core.sheets import SHEETS
from core.text_normalize import normalize_text

ROOT = "تحلیل_جامع_گزارش_حسابرسی"
PART1 = "بخش۱_خلاصه_و_اطلاعات_کلیدی"
PART2 = "بخش۲_تجزیه_تحلیل_گزارش"
PART3 = "بخش۳_چک_لیست_موضوعی"
APPLICABLE = "موضوعیت_دارد"
_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def extract_year(fiscal_period: object) -> int | None:
    """First 4-digit number of the fiscal period (v1 rule), now also for Persian/Arabic digits."""
    if not isinstance(fiscal_period, str):
        return None
    match = re.search(r"(\d{4})", fiscal_period.translate(_TO_LATIN))
    return int(match.group(1)) if match else None


def _text(value: object) -> str | None:
    """LLM value -> stored text (D-7.1): lists joined like v1, everything else str(); Level-A normalized."""
    if value is None:
        return None
    if isinstance(value, list):
        value = ", ".join(map(str, value))
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    elif not isinstance(value, str):
        value = str(value)
    return normalize_text(value)  # type: ignore[return-value]


def _flatten_item(item: dict) -> dict:
    """v1 flatten_reference_data + flatten_array_fields for one row."""
    row = {k: v for k, v in item.items() if k != "ارجاع"}
    if "ارجاع" in item:
        ref = item["ارجاع"] if isinstance(item["ارجاع"], dict) else {}
        row["شماره_بند"] = ref.get("شماره_بند", "")
        row["شماره_صفحه"] = ref.get("شماره_صفحه", "")
    return row


def _section_rows(section: dict, items_key: str, mark_applicable: bool, year: int | None) -> list[dict]:
    if section.get(APPLICABLE, False) and items_key in section:
        rows = [_flatten_item(item) for item in section.get(items_key) or [] if isinstance(item, dict)]
        for row in rows:
            row["year"] = year
            if mark_applicable:
                row[APPLICABLE] = True
        return rows
    return [{APPLICABLE: False, "year": year}]


def flatten(data: dict) -> dict[str, list[dict]]:
    """Persian-keyed rows per sheet, following v1's convert_to_excel logic."""
    report = data.get(ROOT) or {}
    part1 = report.get(PART1) or {}
    year = extract_year(part1.get("دوره_مالی"))
    part2 = report.get(PART2) or {}
    sheets: dict[str, list[dict]] = {spec.key: [] for spec in SHEETS}
    if part1:
        sheets["summary"] = [{**part1, "year": year}]
    if "بند_اظهارنظر" in part2:
        sheets["opinion"] = [{**(part2["بند_اظهارنظر"] or {}), "year": year}]
    if "بند_مبانی_اظهارنظر" in part2:
        sheets["basis"] = _section_rows(part2["بند_مبانی_اظهارنظر"] or {}, "موارد_مطرح_شده", False, year)
    if "بند_تاکید_بر_مطالب_خاص" in part2:
        sheets["emphasis"] = _section_rows(part2["بند_تاکید_بر_مطالب_خاص"] or {}, "موارد_مطرح_شده", True, year)
    if "گزارش_رعایت_الزامات_قانونی" in part2:
        sheets["legal"] = _section_rows(part2["گزارش_رعایت_الزامات_قانونی"] or {}, "تخلفات", True, year)
    if PART3 in report:
        sheets["checklist"] = [{**_flatten_item(item), "year": year} for item in report[PART3] or [] if isinstance(item, dict)]
    return sheets


@dataclass(frozen=True)
class PreparedResult:
    raw_json_gz: bytes
    rows: dict[str, list[dict]]  # sheet key -> DB rows (db column names)
    size_bytes: int
    year: int | None
    unexpected: tuple[str, ...] = ()  # "sheet[row]: keys" where the model's item didn't have the sheet's fields


def prepare(data: dict) -> PreparedResult:
    raw = gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    size = len(raw)
    db_rows: dict[str, list[dict]] = {}
    year = None
    unexpected = []
    flat = flatten(data)
    for spec in SHEETS:
        by_persian = {persian: column for column, persian in spec.columns}
        out = []
        for row_no, row in enumerate(flat[spec.key]):
            # Every column is present (missing values = NULL): the model may omit fields or nest an
            # item differently, and a batch insert needs the same columns in every row.
            record: dict = {"row_no": row_no, **{column: None for column, _ in spec.columns}}
            extra = {}
            for key, value in row.items():
                if key == "year":
                    record["year"] = value
                    year = year or value
                elif key in by_persian:
                    record[by_persian[key]] = _text(value)
                else:
                    extra[key] = value
            record["extra"] = extra or None
            if extra and row.keys() - {"year", APPLICABLE} <= extra.keys():
                unexpected.append(f"{spec.key}[{row_no}]: {sorted(extra)}")
            size += sum(len(str(v).encode("utf-8")) for k, v in record.items() if v is not None and k != "row_no")
            out.append(record)
        db_rows[spec.key] = out
    return PreparedResult(raw, db_rows, size, year, tuple(unexpected))


def gzip_text(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def gunzip_text(data: bytes) -> str:
    return gzip.decompress(data).decode("utf-8")


def save_raw_output(db, *, request_id: str, document_id: str, user_id: int, attempt: int, kind: str,
                    reason: str | None, model_name: str, text: str, input_tokens: int | None,
                    output_tokens: int | None) -> int:
    """Keep the model's answer verbatim (D-11.18). Returns the stored size in bytes."""
    packed = gzip_text(text)
    db.add(RawOutput(request_id=request_id, document_id=document_id, user_id=user_id, attempt=attempt, kind=kind,
                     reason=reason, model_name=model_name, raw_text_gz=packed, size_bytes=len(packed),
                     input_tokens=input_tokens, output_tokens=output_tokens))
    return len(packed)


def raw_outputs(db, request_id: str, document_id: str) -> list[RawOutput]:
    return list(db.scalars(select(RawOutput).where(RawOutput.request_id == request_id,
                                                   RawOutput.document_id == document_id)
                           .order_by(RawOutput.attempt, RawOutput.id)))


def save_result(db, *, request_id: str, document_id: str, user_id: int, provider_name: str, model_name: str,
                api_type: str, temperature_applied: bool, input_tokens: int | None, output_tokens: int | None,
                prepared: PreparedResult, warnings: list[dict] | None = None) -> None:
    db.add(AnalysisResult(
        request_id=request_id, document_id=document_id, user_id=user_id, provider_name=provider_name,
        model_name=model_name, api_type=api_type, temperature_applied=temperature_applied,
        raw_json_gz=prepared.raw_json_gz, size_bytes=prepared.size_bytes,
        input_tokens=input_tokens, output_tokens=output_tokens, warnings=warnings or None,
    ))
    for key, rows in prepared.rows.items():
        if rows:
            db.execute(insert(SHEET_TABLES[key]), [{"request_id": request_id, "document_id": document_id, **r} for r in rows])


def delete_result(db, request_id: str, document_id: str) -> None:
    db.execute(delete(RawOutput).where(RawOutput.request_id == request_id, RawOutput.document_id == document_id))
    for table in SHEET_TABLES.values():
        db.execute(delete(table).where(table.c.request_id == request_id, table.c.document_id == document_id))
    db.execute(delete(AnalysisResult).where(AnalysisResult.request_id == request_id,
                                           AnalysisResult.document_id == document_id))


def load_json(db, request_id: str, document_id: str) -> dict | None:
    result = db.get(AnalysisResult, (request_id, document_id))
    return json.loads(gzip.decompress(result.raw_json_gz)) if result else None


def summary_row(db, request_id: str, document_id: str) -> dict | None:
    table = SHEET_TABLES["summary"]
    row = db.execute(select(table).where(table.c.request_id == request_id, table.c.document_id == document_id)).first()
    return dict(row._mapping) if row else None


def summary_from_data(data: dict) -> dict:
    """Summary fields straight from model JSON (for results that are not stored, e.g. guests)."""
    part1 = (data.get(ROOT) or {}).get(PART1) or {}
    return {
        "company_name": _text(part1.get("نام_شرکت")),
        "auditor_name": _text(part1.get("نام_حسابرس")),
        "fiscal_period": _text(part1.get("دوره_مالی")),
        "opinion_type": _text(part1.get("نوع_اظهارنظر")),
        "overall_risk": _text(part1.get("سطح_ریسک_کلی_بنا_به_گزارش")),
        "year": extract_year(part1.get("دوره_مالی")),
    }
