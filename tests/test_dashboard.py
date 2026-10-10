"""Results dashboard rules (D-13): source, duplicates, company keys and merges, chart data."""

from datetime import UTC, datetime, timedelta

from test_jobs import env, pdf, statuses  # noqa: F401  (shared engine fixture)

from core.models import Role
from services import dashboard as dash
from services import history
from services import providers as pv
from services.auth import load_active_user
from ui import charts


def record(doc_id, company, year, minutes=0, **rows):
    summary = {"company_name": company, "year": year, "opinion_type": "مقبول", "overall_risk": "متوسط"}
    return dash.DocRecord("R", "R", doc_id, f"{doc_id}.pdf", datetime(2026, 10, 9, tzinfo=UTC) + timedelta(minutes=minutes),
                          "P › m", {"summary": [summary], **rows})


def test_company_key_ignores_legal_form_and_letter_variants():
    same = ["بانک ملت", "بانك ملت (سهامی عام)", "شرکت بانک ملت", "بانک  ملت (سهامي خاص)", "بانک ملت با مسئولیت محدود"]
    assert {dash.company_key(n) for n in same} == {"بانک ملت"}
    assert dash.company_key("بانک ملت") != dash.company_key("بانک تجارت")
    assert dash.ordinal("بحراني", dash.RISKS) == 3 and dash.ordinal("نامعلوم", dash.RISKS) is None


def test_one_document_per_company_and_year():
    a = record("a", "بانک ملت", 1398, minutes=1)
    b = record("b", "بانک ملت (سهامی عام)", 1398, minutes=5)  # newer, same company and year
    c = record("c", "بانک ملت", 1399)
    undated = record("d", "بانک ملت", None)
    group = dash.group_of({})
    chosen, alternates = dash.choose_per_year([a, b, c, undated], group)
    assert {r.document_id for r in chosen} == {"b", "c", "d"}
    assert [r.document_id for r in alternates[("بانک ملت", 1398)]] == ["b", "a"]
    chosen, _ = dash.choose_per_year([a, b, c], group, {("بانک ملت", 1398): "a"})
    assert {r.document_id for r in chosen} == {"a", "c"}


def test_merges_are_saved_per_user_and_never_chain(env):
    ali, sara = env.user("ali", Role.ANALYST), env.user("sara", Role.ANALYST)
    dash.save_merge(ali.id, "بانک ملت", ["بانک ملت ایران"])
    dash.save_merge(ali.id, "بانک ملل", ["بانک ملت"])  # the old main moves, with its members
    assert dash.merges(ali.id) == {"بانک ملت": "بانک ملل", "بانک ملت ایران": "بانک ملل"}
    assert dash.merges(sara.id) == {}
    records = [record("a", "بانک ملت", 1398), record("b", "بانک ملت ایران", 1399), record("c", "بانک ملل", 1400)]
    companies = dash.group_companies(records, dash.merges(ali.id))
    assert list(companies) == ["بانک ملل"] and companies["بانک ملل"].name == "بانک ملل"
    assert companies["بانک ملل"].years == [1398, 1399, 1400]
    dash.remove_group(ali.id, "بانک ملل")
    assert dash.merges(ali.id) == {}


def test_heatmap_topics():
    rows = [{"topic": "کفایت سرمایه", "status": "ریسک بحرانی"},
            {"topic": "تداوم فعالیت", "status": "مصداق ندارد"},
            {"topic": "دعاوی و جرائم حقوقی", "status": "مسئله کلیدی منجر به اظهارنظر مشروط"},
            {"topic": dash.SIGNATURE_TOPIC, "status": "ریسک بحرانی"}]
    records = [record("a", "x", 1398, checklist=rows), record("b", "x", 1399, checklist=rows[:1])]
    topics, years, matrix = dash.heatmap(records)
    # v1 dropped «دعاوی و جرائم حقوقی» (not in its 24-topic list); the signature page is never a risk
    assert topics == ["کفایت سرمایه", "دعاوی و جرائم حقوقی"] and years == [1398, 1399]
    assert matrix == [[3, 3], [2, 0]]
    all_topics, _, _ = dash.heatmap(records, show_all=True)
    assert len(all_topics) == len(dash.ALL_TOPICS) - 1 and all_topics[:24] == list(dash.HEATMAP_PRIORITY)


def test_load_all_counts_each_pdf_once_and_respects_sharing(env):
    ali = env.user("ali", Role.ANALYST)
    viewer = env.user("viw", Role.VIEWER)
    first = env.run(ali, [pdf("1398.pdf"), pdf("1399.pdf")])
    pv.save_model(None, env.provider.id, pv.ModelForm("mock-2", "مدل دوم"))
    other = next(c for c in pv.usable_models(ali.id) if c.model_name == "mock-2")
    second = env.run(ali, [pdf("1398.pdf")], choice=other)  # the same PDF analysed again
    loaded = dash.load_all(ali)
    assert sorted(r.year for r in loaded) == [1398, 1399]
    assert next(r for r in loaded if r.year == 1398).result_request_id == second.id  # the newest result (D-13.2)
    assert dash.load_all(viewer) == []
    history.set_shares(ali, first.id, [viewer.id])
    shared = dash.load_all(viewer)
    assert sorted(r.year for r in shared) == [1398, 1399] and {r.request_id for r in shared} == {first.id}
    assert len(dash.load_all(load_active_user(1))) == 2  # admins: everyone's, still once per PDF
    assert [r.result_request_id for r in dash.load_request(first)] == [first.id, first.id]
    assert dash.load_all(ali)[0].rows["checklist"]  # sheet rows are loaded


def test_guest_records_from_memory(env):
    guest = env.user("gst", Role.GUEST, guest_doc_cap=3)
    state = env.run(guest, [pdf("1398.pdf")])
    (rec,) = dash.load_request(state)
    assert rec.result_request_id is None and rec.year == 1398 and rec.rows["checklist"]


def test_chart_builders():
    fig = charts.trend([(1398, 0), (1398, 3), (1399, 2)], dash.RISKS, "#c0392b", "سطح ریسک")
    assert list(fig.data[1].y) == [2, 2]  # median 1.5 rounds to 2 (v1: pandas round), then 2
    rows = [{"year": 1398, "risk_category": "ریسک اعتباری", "risk_subcategory": "تمرکز"},
            {"year": 1398, "risk_category": "ریسک اعتباری", "risk_subcategory": "نکول"},
            {"year": 1399, "risk_category": "ریسک بازار", "risk_subcategory": None}]
    bars = charts.stacked_bars(rows, "risk_category", dash.RISK_CATEGORIES, "دسته اصلی ریسک", "تعداد")
    assert [(t.name, list(t.y)) for t in bars.data] == [("ریسک اعتباری", [2, 0]), ("ریسک بازار", [0, 1])]
    sun = charts.sunburst(rows, "risk_category", "risk_subcategory", "ریسک‌ها")
    assert sun.data[0].values[0] == 3
    heat = charts.status_heatmap(["کفایت سرمایه"], [1398], [[3]])
    assert heat.data[0].customdata[0][0] == "ریسک بحرانی"
