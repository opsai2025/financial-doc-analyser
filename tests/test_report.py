"""Word report of a request (D-16.1, W1–W4)."""

import io

from docx import Document
from test_jobs import env, pdf  # noqa: F401  (shared engine fixture)

from core.models import Role
from services import excel, report, results
from services import providers as pv
from services.dashboard import applicable


def open_report(state):
    return Document(io.BytesIO(report.build_report(state)))


def header(table) -> list[str]:
    return [c.text for c in table.rows[0].cells]


def test_report_layout_and_content(env):
    ali = env.user("ali", Role.ANALYST)
    state = env.run(ali, [pdf("1398.pdf"), pdf("bad.pdf"), pdf("1399.pdf")])
    doc = open_report(state)

    assert doc.paragraphs[0].text == report.TITLE
    sections = [p.text for p in doc.paragraphs if p.style.name == "Heading 2"]
    assert sections[:2] == ["خلاصه اسناد", "اسناد بدون نتیجه"] and len(sections) == 4  # + one per document
    assert sections[2].startswith("بانک نمونه") and sections[2].endswith("۱۳۹۸")

    overview = next(t for t in doc.tables if header(t)[0] == "ردیف")
    # file names keep their own direction: "1398.pdf", not "pdf.1398" (left-to-right embedding)
    assert [r.cells[5].text for r in overview.rows[1:]] == ["‪1398.pdf‬", "‪1399.pdf‬"]
    failed = next(t for t in doc.tables if header(t) == ["فایل", "وضعیت", "توضیح"])
    assert failed.rows[1].cells[0].text == "‪bad.pdf‬" and failed.rows[1].cells[1].text == "ناموفق"
    checklist = [t for t in doc.tables if header(t)[:2] == ["موضوع", "در گزارش آمده"]]
    assert {r.cells[1].text for t in checklist for r in t.rows[1:]} <= {"بله", "خیر", ""}

    # W1/W2: every item table, applicable items only (the dashboard's rule)
    rows = [d.rows for d in excel.collect_docs(state) if d.rows]
    for sheet, _, columns in report.ITEM_TABLES:
        labels = [label for label, _, _ in columns]
        shown = sum(len(t.rows) - 1 for t in doc.tables if header(t) == labels)
        expected = sum(1 for r in rows for row in r[sheet]
                       if sheet not in report.APPLICABILITY_SHEETS or applicable(row))
        assert shown == expected, sheet

    # Right-to-left: paragraphs, tables, Vazirmatn for Persian (complex script) text
    body = doc.element.body.xml
    assert "w:bidiVisual" in body and "w:tblHeader" in body
    normal = doc.styles["Normal"].element.xml
    assert "w:bidi" in normal and 'w:cs="Vazirmatn"' in normal and "w:szCs" in normal


def test_guest_report_from_memory_without_control_characters(env):
    guest = env.user("gst", Role.GUEST, guest_doc_cap=3)
    pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock", "mock://?delay=0"), env.provider.id)
    state = env.run(guest, [pdf("1398.pdf")])
    state.docs[0].data[results.ROOT][results.PART1]["نکات_کلیدی_و_نتیجه_گیری"] = "نکته\x07 مهم\nسطر دوم"
    doc = open_report(state)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "نکته مهم\nسطر دوم" in text and "\x07" not in text
    assert report.report_name(state) == f"{state.id}.docx"
