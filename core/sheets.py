"""The six v1 Excel sheets (D-8.2): stored as one DB table each, linked by (request_id, document_id).

Column order follows what v1 wrote to Excel. Python-produced columns (`year`) are typed; every
LLM-produced value is stored as text (D-7.1).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SheetSpec:
    key: str
    table: str
    title: str  # v1 Excel sheet name
    columns: tuple[tuple[str, str], ...]  # (db column, Persian column name as in v1)


YEAR = ("year", "year")

SHEETS: tuple[SheetSpec, ...] = (
    SheetSpec(
        "summary", "sheet_summary", "بخش1_خلاصه",
        (
            ("company_name", "نام_شرکت"),
            ("auditor_name", "نام_حسابرس"),
            ("fiscal_period", "دوره_مالی"),
            ("opinion_type", "نوع_اظهارنظر"),
            ("overall_risk", "سطح_ریسک_کلی_بنا_به_گزارش"),
            ("risk_details", "جزییات_سطح_ریسک_تعیین_شده"),
            ("key_points", "نکات_کلیدی_و_نتیجه_گیری"),
            YEAR,
        ),
    ),
    SheetSpec(
        "opinion", "sheet_opinion", "بند_اظهارنظر",
        (("opinion_kind", "نوع"), ("reasons_summary", "خلاصه_دلایل"), YEAR),
    ),
    SheetSpec(
        "basis", "sheet_basis", "بند_مبانی_اظهارنظر",
        (
            ("item_no", "شماره_مورد"),
            ("title", "عنوان"),
            ("description", "شرح"),
            ("reason_type", "نوع_دلیل"),
            ("paragraph_no", "شماره_بند"),
            ("page_no", "شماره_صفحه"),
            YEAR,
            ("is_applicable", "موضوعیت_دارد"),
        ),
    ),
    SheetSpec(
        "emphasis", "sheet_emphasis", "بند_تاکید_بر_مطالب_خاص",
        (
            ("title", "عنوان"),
            ("description", "شرح"),
            ("highlighted_risk", "ریسک_برجسته_شده"),
            ("risk_category", "دسته_اصلی_ریسک"),
            ("risk_subcategory", "زیرشاخه_ریسک"),
            ("paragraph_no", "شماره_بند"),
            ("page_no", "شماره_صفحه"),
            YEAR,
            ("is_applicable", "موضوعیت_دارد"),
        ),
    ),
    SheetSpec(
        "legal", "sheet_legal", "گزارش_قانونی",
        (
            ("violation_title", "عنوان_تخلف"),
            ("description", "شرح"),
            ("legal_bases", "مبانی_قانونی_و_استانداردها"),
            ("category", "دسته_اصلی"),
            ("subcategory", "زیرشاخه"),
            ("paragraph_no", "شماره_بند"),
            ("page_no", "شماره_صفحه"),
            YEAR,
            ("is_applicable", "موضوعیت_دارد"),
        ),
    ),
    SheetSpec(
        "checklist", "sheet_checklist", "بخش3_چک_لیست",
        (
            ("topic", "موضوع"),
            ("in_report", "در_گزارش_آمده"),
            ("status", "وضعیت"),
            ("details", "جزئیات"),
            ("paragraph_no", "شماره_بند"),
            ("page_no", "شماره_صفحه"),
            YEAR,
        ),
    ),
)

SHEETS_BY_KEY = {s.key: s for s in SHEETS}
