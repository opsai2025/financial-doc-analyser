"""Word report of one request (D-16.1, W1–W4).

Built on demand from the same rows as the Excel workbook and never stored (D-3.3). A4 portrait,
right-to-left, Vazirmatn: a cover with the request's details and a summary table, then one
section per document with everything the Excel has, laid out for reading. Items marked "not
applicable" are left out (they stay in Excel); no charts.
"""

from __future__ import annotations

import io
import unicodedata

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from core.fa import fa_digits, jalali_datetime
from core.models import utcnow
from services.dashboard import applicable
from services.excel import DocRows, collect_docs
from services.jobs import RequestState, doc_explanation, doc_status_label

FONT = "Vazirmatn"
NAVY = RGBColor(0x0F, 0x17, 0x2A)
MUTED = RGBColor(0x52, 0x60, 0x7A)
HEADER_FILL = "CCC0DA"  # the Excel header colour (D-16.2 keeps the v1 look)
TITLE = "گزارش تحلیل گزارش‌های حسابرسی"
NOT_IN_EXCEL_NOTE = ("متن‌ها همان پاسخ مدل است. مواردی که مدل «موضوعیت ندارد» تشخیص داده در این گزارش نیامده‌اند "
                     "و در فایل اکسل همین درخواست موجودند.")
NONE_FOUND = "موردی ثبت نشده است."
LRE, RLE, POP = "\u202a", "\u202b", "\u202c"  # left/right-to-left embedding, pop

# Item tables per sheet: (heading, [(label, columns joined with " / ", width in cm)])
ITEM_TABLES = (
    ("basis", "مبانی اظهارنظر", (("شماره", ("item_no",), 1.4), ("عنوان", ("title",), 3.4), ("شرح", ("description",), 7.2),
                                 ("نوع دلیل", ("reason_type",), 2.4), ("بند / صفحه", ("paragraph_no", "page_no"), 2.2))),
    ("emphasis", "تأکید بر مطالب خاص", (("عنوان", ("title",), 3.2), ("شرح", ("description",), 6.4),
                                        ("ریسک برجسته‌شده", ("highlighted_risk",), 2.8),
                                        ("دسته / زیرشاخه", ("risk_category", "risk_subcategory"), 2.6),
                                        ("بند / صفحه", ("paragraph_no", "page_no"), 1.6))),
    ("legal", "گزارش قانونی (تخلفات)", (("عنوان تخلف", ("violation_title",), 3.2), ("شرح", ("description",), 5.6),
                                        ("مبانی قانونی", ("legal_bases",), 3.4),
                                        ("دسته / زیرشاخه", ("category", "subcategory"), 2.6),
                                        ("بند / صفحه", ("paragraph_no", "page_no"), 1.8))),
    ("checklist", "چک‌لیست", (("موضوع", ("topic",), 3.6), ("در گزارش آمده", ("in_report",), 2.0),
                              ("وضعیت", ("status",), 2.4), ("جزئیات", ("details",), 7.0),
                              ("بند / صفحه", ("paragraph_no", "page_no"), 1.6))),
)
APPLICABILITY_SHEETS = ("basis", "emphasis", "legal")  # sheets with «موضوعیت_دارد» (as the dashboard)


def report_name(state: RequestState) -> str:
    return f"{state.id}.docx"


def _text(value) -> str:
    """Word-safe text: no control characters (python-docx rejects them)."""
    if value is None:
        return ""
    return ILLEGAL_CHARACTERS_RE.sub("", value if isinstance(value, str) else str(value)).strip()


def _file(name) -> str:
    """A file name in its own direction, so "1400.pdf" isn't shown as "pdf.1400" (as D-12.16 in the app).

    Word doesn't reliably support the isolate marks the app uses; an embedding (LRE/RLE … PDF) chosen
    from the first strong character works in every version.
    """
    name = _text(name)
    for char in name:
        kind = unicodedata.bidirectional(char)
        if kind in ("R", "AL"):
            return f"{RLE}{name}{POP}"
        if kind == "L":
            break
    return f"{LRE}{name}{POP}" if name else ""


def _yes_no(value) -> str:
    """Python-style flags the model's answers carry as text («در_گزارش_آمده»), shown in Persian."""
    text = _text(value)
    return {"True": "بله", "False": "خیر"}.get(text, text)


# --- Right-to-left formatting (python-docx has no API for most of it) -------------------------

def _insert(parent, tag: str, successors: tuple[str, ...]):
    """The child `tag` of `parent`, created in schema order when missing."""
    found = parent.find(qn(tag))
    if found is None:
        found = OxmlElement(tag)
        parent.insert_element_before(found, *successors)
    return found


RPR_AFTER_SZCS = ("w:highlight", "w:u", "w:effect", "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs",
                  "w:em", "w:lang", "w:eastAsianLayout", "w:specVanish", "w:oMath")
PPR_AFTER_BIDI = ("w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
                  "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment", "w:textboxTightWrap",
                  "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange")


def _font(rpr, *, size: float | None = None, bold: bool | None = None, color: RGBColor | None = None) -> None:
    """Vazirmatn for every script, right-to-left; size and bold also for complex scripts (Persian)."""
    fonts = rpr.get_or_add_rFonts()
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        fonts.attrib.pop(qn(attr), None)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(attr), FONT)
    rpr.get_or_add_rtl().set(qn("w:val"), "1")
    if bold is not None:
        rpr.get_or_add_b().set(qn("w:val"), "1" if bold else "0")
        rpr.get_or_add_bCs().set(qn("w:val"), "1" if bold else "0")
    if color is not None:
        rpr.get_or_add_color().set(qn("w:val"), str(color))
    if size is not None:
        half_points = str(int(size * 2))
        rpr.get_or_add_sz().set(qn("w:val"), half_points)
        _insert(rpr, "w:szCs", RPR_AFTER_SZCS).set(qn("w:val"), half_points)


def _bidi(ppr) -> None:
    _insert(ppr, "w:bidi", PPR_AFTER_BIDI)


def _style(document, name: str, *, size: float, bold: bool = False, color: RGBColor | None = None,
           before: float = 0, after: float = 4):
    style = document.styles[name]
    _font(style.element.get_or_add_rPr(), size=size, bold=bold, color=color)
    _bidi(style.element.get_or_add_pPr())
    style.paragraph_format.space_before, style.paragraph_format.space_after = Pt(before), Pt(after)
    return style


def _new_document():
    document = Document()
    section = document.sections[0]
    section.page_height, section.page_width = Cm(29.7), Cm(21.0)
    section.top_margin = section.bottom_margin = Cm(2.0)
    section.left_margin = section.right_margin = Cm(2.0)
    _style(document, "Normal", size=10.5, after=4)
    _style(document, "Heading 1", size=17, bold=True, color=NAVY, after=8)
    _style(document, "Heading 2", size=14, bold=True, color=NAVY, before=12, after=6)
    _style(document, "Heading 3", size=11.5, bold=True, color=NAVY, before=10, after=4)
    return document


def _paragraph(document, text: str, *, style: str | None = None, muted: bool = False, bold: bool = False):
    paragraph = document.add_paragraph(style=style)
    run = paragraph.add_run(text)
    if muted or bold:
        _font(run._r.get_or_add_rPr(), color=MUTED if muted else None, bold=bold or None)
    return paragraph


def _cell(cell, text: str, *, header: bool = False, size: float = 9.5) -> None:
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run(text)
    _font(run._r.get_or_add_rPr(), size=size, bold=header or None)
    if header:
        tcpr = cell._tc.get_or_add_tcPr()
        shading = _insert(tcpr, "w:shd", ("w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign",
                                          "w:hideMark", "w:headers", "w:cellIns", "w:cellDel", "w:cellMerge",
                                          "w:tcPrChange"))
        shading.set(qn("w:val"), "clear")
        shading.set(qn("w:color"), "auto")
        shading.set(qn("w:fill"), HEADER_FILL)


def _table(document, headers: list[str] | None, rows: list[list[str]], widths_cm: list[float]):
    """A bordered right-to-left table (first column on the right); the header row repeats on every page."""
    table = document.add_table(rows=0, cols=len(widths_cm))
    table.style = document.styles["Table Grid"]
    table.autofit = False
    _insert(table._tbl.tblPr, "w:bidiVisual", ("w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc",
                                                "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd",
                                                "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption",
                                                "w:tblDescription", "w:tblPrChange"))
    lines = ([headers] if headers else []) + rows
    for index, line in enumerate(lines):
        row = table.add_row()
        header = headers is not None and index == 0
        if header:
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for cell, value, width in zip(row.cells, line, widths_cm, strict=True):
            cell.width = Cm(width)
            _cell(cell, value, header=header)
    document.add_paragraph().paragraph_format.space_after = Pt(2)
    return table


def _key_values(document, pairs: list[tuple[str, str]]) -> None:
    table = _table(document, None, [[label, value] for label, value in pairs if value], [4.0, 13.0])
    for row in table.rows:
        _font(row.cells[0].paragraphs[0].runs[0]._r.get_or_add_rPr(), bold=True)


# --- Content ---------------------------------------------------------------------------------

def _summary(d: DocRows) -> dict:
    return ((d.rows or {}).get("summary") or [{}])[0]


def _company(d: DocRows) -> str:
    return _text(_summary(d).get("company_name") or (d.doc.summary or {}).get("company_name")) or "نامشخص"


def _year(d: DocRows) -> str:
    year = _summary(d).get("year") or (d.doc.summary or {}).get("year")
    return fa_digits(year) if year else ""


def _joined(row: dict, columns: tuple[str, ...]) -> str:
    return " / ".join(t for t in ((_yes_no if c == "in_report" else _text)(row.get(c)) for c in columns) if t)


def _cover(document, state: RequestState, with_result: list[DocRows], without: list[DocRows]) -> None:
    title = document.add_paragraph(TITLE, style="Heading 1")
    title.paragraph_format.alignment = 1  # center
    counts = [f"{fa_digits(len(with_result))} سند با نتیجه"]
    if without:
        counts.append(f"{fa_digits(len(without))} سند بدون نتیجه")
    models = sorted({f"{d.provider} › {d.model}" for d in with_result}) or [f"{state.provider_name} › {state.model_name}"]
    _key_values(document, [
        ("شناسه درخواست", state.id),
        ("کاربر", _text(state.username)),
        ("زمان ایجاد درخواست", jalali_datetime(state.created_at)),
        ("ارائه‌دهنده › مدل", "، ".join(_text(m) for m in models)),
        ("اسناد", "، ".join(counts)),
        ("زمان تهیه گزارش", jalali_datetime(utcnow())),
    ])

    document.add_paragraph("خلاصه اسناد", style="Heading 2")
    if with_result:
        _table(document, ["ردیف", "شرکت", "سال", "نوع اظهارنظر", "سطح ریسک کلی", "فایل"], [
            [fa_digits(number), _company(d), _year(d), _text(_summary(d).get("opinion_type")),
             _text(_summary(d).get("overall_risk")), _file(d.doc.file_name)]
            for number, d in enumerate(with_result, start=1)
        ], [1.2, 4.4, 1.4, 3.0, 2.6, 4.4])
    else:
        _paragraph(document, "هیچ سندی از این درخواست نتیجه ندارد.", muted=True)

    if without:
        document.add_paragraph("اسناد بدون نتیجه", style="Heading 2")
        rows = []
        for d in without:
            explanation = doc_explanation(d.doc)
            if d.rows is None and d.doc.status in ("succeeded", "reused"):
                explanation = " ".join(filter(None, [explanation, "نتیجه این سند دیگر در دسترس نیست."]))
            rows.append([_file(d.doc.file_name), doc_status_label(d.doc), _text(explanation)])
        _table(document, ["فایل", "وضعیت", "توضیح"], rows, [4.6, 3.0, 9.4])
    _paragraph(document, NOT_IN_EXCEL_NOTE, muted=True)


def _document_section(document, d: DocRows) -> None:
    summary = _summary(d)
    year = _year(d)
    document.add_paragraph(f"{_company(d)} — {year}" if year else _company(d), style="Heading 2")
    _key_values(document, [
        ("حسابرس", _text(summary.get("auditor_name"))),
        ("دوره مالی", _text(summary.get("fiscal_period"))),
        ("نوع اظهارنظر", _text(summary.get("opinion_type"))),
        ("سطح ریسک کلی", _text(summary.get("overall_risk"))),
        ("فایل", _file(d.doc.file_name)),
        ("مدل", _text(f"{d.provider} › {d.model}")),
        ("زمان تحلیل", jalali_datetime(d.analysed_at) if d.analysed_at else ""),
    ])
    for heading, column in (("نکات کلیدی و نتیجه‌گیری", "key_points"), ("جزییات سطح ریسک", "risk_details")):
        document.add_paragraph(heading, style="Heading 3")
        _paragraph(document, _text(summary.get(column)) or NONE_FOUND, muted=not summary.get(column))

    document.add_paragraph("بند اظهارنظر", style="Heading 3")
    opinions = [r for r in d.rows.get("opinion", []) if _text(r.get("opinion_kind")) or _text(r.get("reasons_summary"))]
    if opinions:
        _table(document, ["نوع", "خلاصه دلایل"], [[_text(r.get("opinion_kind")), _text(r.get("reasons_summary"))]
                                                   for r in opinions], [3.0, 14.0])
    else:
        _paragraph(document, NONE_FOUND, muted=True)

    for sheet, heading, columns in ITEM_TABLES:
        document.add_paragraph(heading, style="Heading 3")
        rows = [r for r in d.rows.get(sheet, []) if sheet not in APPLICABILITY_SHEETS or applicable(r)]
        if not rows:
            _paragraph(document, NONE_FOUND, muted=True)
            continue
        _table(document, [label for label, _, _ in columns],
               [[_joined(r, cols) for _, cols, _ in columns] for r in rows], [w for _, _, w in columns])


def build_report(state: RequestState) -> bytes:
    docs = collect_docs(state)
    with_result = [d for d in docs if d.rows]
    without = [d for d in docs if not d.rows]
    document = _new_document()
    footer = document.sections[0].footer.paragraphs[0]
    footer.add_run(f"{TITLE} · {state.id}")
    _font(footer.runs[0]._r.get_or_add_rPr(), size=8, color=MUTED)
    _cover(document, state, with_result, without)
    for d in with_result:
        document.add_page_break()
        _document_section(document, d)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()

