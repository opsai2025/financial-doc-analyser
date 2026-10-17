import hashlib
from collections import Counter

import pandas as pd
import streamlit as st

from core.fa import fa_digits, fa_number, jalali_datetime
from core.models import Role
from core.sheets import SHEETS_BY_KEY
from services import dashboard as dash
from services import history
from services.jobs import get_job_manager
from ui import charts
from ui.components import Card, cards_row, help_box, md_escape, page_title
from ui.session import require_user

user = require_user()
manager = get_job_manager()

page_title("داشبورد نتایج", "monitoring", "روند ریسک و اظهارنظر، ریسک‌های برجسته، تخلفات قانونی و موضوعات کلیدی")

ALL, SHARED = "all", "shared"


def short(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def years_text(years: list[int]) -> str:
    if not years:
        return "بدون سال"
    return fa_digits(years[0]) if len(years) == 1 else f"{fa_digits(years[0])}–{fa_digits(years[-1])}"


def show_chart(fig, key: str) -> None:
    st.plotly_chart(fig, config=charts.CONFIG, key=key)


def explain_box(key: str, explain: str) -> None:
    help_box("توضیحات نمودار", charts.EXPLAIN[explain], variant="blue", icon="help", expanded=False, key=f"{key}_help")


def chart_block(title: str, fig, key: str, explain: str) -> None:
    st.markdown(f"**{title}**")
    show_chart(fig, key)
    explain_box(key, explain)


def chart_pair(*charts_: tuple | str) -> None:
    """Two charts side by side, then their explanations in a row of their own, aligned (D-14.11).

    Each item is (title, figure, key, explanation id), or a Persian message when there is no data.
    """
    columns = st.columns(2, gap="large")
    for column, item in zip(columns, charts_, strict=True):
        with column:
            if isinstance(item, str):
                st.info(item, icon=":material/info:")
            else:
                st.markdown(f"**{item[0]}**")
                show_chart(item[1], item[2])
    columns = st.columns(2, gap="large")
    for column, item in zip(columns, charts_, strict=True):
        if not isinstance(item, str):
            with column:
                explain_box(item[2], item[3])


# ---- Source (D-13.1) ---------------------------------------------------------------------------
if user.role is Role.GUEST:
    state = manager.latest_ephemeral(user.id)
    if state is None:
        st.info("هنوز درخواستی ثبت نکرده‌اید یا نتایج آخرین درخواست شما منقضی شده است.", icon=":material/info:")
        st.stop()
    st.caption(f":material/source: منبع: آخرین درخواست شما `{state.id}` (نتایج مهمان ذخیره نمی‌شوند).")
    records = dash.load_request(state)
else:
    viewer = user.role is Role.VIEWER
    own = [] if viewer else history.search_requests(user, limit=300)
    shared_requests = history.search_requests(user, shared=True, limit=300) if not user.is_admin else []
    labels = {}
    if not viewer:
        labels[ALL] = "همه نتایج (همه کاربران)" if user.is_admin else "همه نتایج من"
    if viewer or shared_requests:
        labels[SHARED] = "همه درخواست‌های اشتراک‌گذاشته با من"
    labels |= {r.id: f"{r.id} — {jalali_datetime(r.created_at)} — {r.model_label}"
                     + (f" — {r.username}" if user.is_admin else "") for r in own}
    labels |= {r.id: f"{r.id} — {jalali_datetime(r.created_at)} — {r.model_label} — اشتراکی از {r.username}"
               for r in shared_requests}
    opened = st.session_state.pop("dash_open", None)  # "open in dashboard" from a request (D-13.1)
    if opened in labels:
        st.session_state.dash_source = opened
    if st.session_state.get("dash_source") not in labels:
        st.session_state.dash_source = next(iter(labels))
    source = st.selectbox("منبع داده", list(labels), format_func=labels.get, key="dash_source")
    if source in (ALL, SHARED):
        records = dash.load_all(user, shared=source == SHARED)
        st.caption("از هر فایل PDF فقط جدیدترین نتیجه شمرده می‌شود.")
    else:
        state = manager.get(source, user)
        records = dash.load_request(state) if state else []

if not records:
    st.info("هنوز نتیجه موفقی برای نمایش وجود ندارد. پس از تحلیل اسناد، نتایج اینجا نمایش داده می‌شوند.",
            icon=":material/insights:")
    st.stop()

merge_map = dash.merges(user.id) if user.role is not Role.GUEST else {}
companies = dash.group_companies(records, merge_map)
group = dash.group_of(merge_map)
overrides: dict = st.session_state.setdefault("dash_year_choice", {})
chosen, alternates = dash.choose_per_year(records, group, overrides)


def summary_table(rows: list[dash.DocRecord], with_company: bool) -> None:
    table = pd.DataFrame([{
        **({"شرکت": companies[group(r)].name} if with_company else {}),
        "سال": r.year,
        "دوره مالی": r.summary.get("fiscal_period") or "",
        "حسابرس": r.summary.get("auditor_name") or "",
        "اظهارنظر": r.summary.get("opinion_type") or "",
        "سطح ریسک": r.summary.get("overall_risk") or "",
        "فایل": r.file_name,
        "درخواست": r.request_id,
    } for r in sorted(rows, key=lambda r: (companies[group(r)].name, r.year or 0))])
    st.dataframe(table[table.columns[::-1]], hide_index=True, column_config={
        "سال": st.column_config.NumberColumn(format="%d", width="small"),
        "شرکت": st.column_config.TextColumn(width="medium"),
        "دوره مالی": st.column_config.TextColumn(width="medium")})


# ---- Overview (D-13.6) -----------------------------------------------------------------------------
def overview() -> None:
    risks = dash.distribution(chosen, "overall_risk", dash.RISKS)
    opinions = dash.distribution(chosen, "opinion_type", dash.OPINIONS)
    high = risks["بالا"] + risks["بحرانی"]
    not_clean = sum(n for name, n in opinions.items() if name != "مقبول")
    cards_row([
        Card("شرکت‌ها", fa_number(len(companies)), "domain", "info"),
        Card("اسناد", fa_number(len(chosen)), "description", "info"),
        Card("ریسک بالا یا بحرانی", fa_number(high), "warning", "danger" if high else "neutral"),
        Card("اظهارنظر مقبول", fa_number(opinions["مقبول"]), "verified", "success"),
        Card("اظهارنظر غیرمقبول", fa_number(not_clean), "gpp_maybe", "warning" if not_clean else "neutral"),
    ], key="dash_overview")
    if alternates:
        st.caption(f":material/content_copy: برای {fa_number(len(alternates))} مورد شرکت و سال بیش از یک سند وجود دارد؛ "
                   "از هر سال فقط یک سند شمرده می‌شود (قابل انتخاب در زبانه «تحلیل یک شرکت»).")
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown("**توزیع سطح ریسک کلی**")
        show_chart(charts.risk_distribution(risks), "dash_ov_risk")
    with c2:
        st.markdown("**توزیع نوع اظهارنظر**")
        show_chart(charts.opinion_distribution(opinions), "dash_ov_opinion")
    st.subheader(":material/table: جدول خلاصه", anchor=False)
    summary_table(chosen, with_company=True)


# ---- One company (D-13.6–D-13.9) -------------------------------------------------------------------
def tone(position: int | None) -> str:
    if position is None:
        return "neutral"
    return "success" if position == 0 else "warning" if position == 1 else "danger"


def year_choices(key: str) -> None:
    """Years with several documents: pick the one that counts (D-13.3)."""
    mine = sorted((k, v) for k, v in alternates.items() if k[0] == key)
    if not mine:
        return
    with st.expander(f":material/content_copy: {fa_number(len(mine))} سال با بیش از یک سند — از هر سال فقط یک سند "
                     "شمرده می‌شود"):
        st.caption("به‌طور پیش‌فرض جدیدترین سند تحلیل‌شده هر سال استفاده می‌شود. انتخاب شما تا پایان این نشست باقی می‌ماند.")
        for (group_key, year), candidates in mine:
            names = {c.document_id: f"{c.file_name} — {jalali_datetime(c.analysed_at)} — {c.model_label}"
                     for c in candidates}
            picked = st.selectbox(f"سال {fa_digits(year)}", list(names), format_func=names.get,
                                  key=f"dash_alt_{short(group_key)}_{year}")
            if picked != candidates[0].document_id:
                overrides[(group_key, year)] = picked
                st.rerun()


def item_table(rows: list[dict], sheet: str) -> None:
    spec = SHEETS_BY_KEY[sheet]
    columns = [(c, p) for c, p in spec.columns if c not in ("year", "is_applicable")]
    if not rows:
        st.info("موردی برای سال‌های انتخاب‌شده وجود ندارد.", icon=":material/info:")
        return
    table = pd.DataFrame([{"سال": r.get("year"), **{p: r.get(c) for c, p in columns}}
                          for r in sorted(rows, key=lambda r: r.get("year") or 0)])
    st.dataframe(table[table.columns[::-1]], hide_index=True, column_config={
        "سال": st.column_config.NumberColumn(format="%d", width="small")})


def company_view() -> None:
    ordered = sorted(companies.values(), key=lambda c: (-len(c.records), c.name))
    names = {c.key: f"{c.name} ({fa_number(len(c.records))} سند، {years_text(c.years)})"
             + (" — ادغام‌شده" if len(c.member_keys) > 1 else "") for c in ordered}
    if st.session_state.get("dash_company") not in names:
        st.session_state.dash_company = ordered[0].key
    key = st.selectbox("شرکت", list(names), format_func=names.get, key="dash_company")
    company = companies[key]
    rows = [r for r in chosen if group(r) == key]
    year_choices(key)
    dated = sorted((r for r in rows if r.year is not None), key=lambda r: r.year)
    undated = [r for r in rows if r.year is None]
    if undated:
        st.caption(":material/event_busy: سال مالی این اسناد تشخیص داده نشد و در نمودارهای روند نمی‌آیند: "
                   + "، ".join(md_escape(r.file_name) for r in undated))

    latest = dated[-1] if dated else None
    emphasis_rows = dash.items(rows, "emphasis")
    legal_rows = dash.items(rows, "legal")
    latest_year = latest.year if latest else None
    latest_help = f"در آخرین سال ({fa_digits(latest_year)})" if latest_year else "سال مالی مشخص نیست"
    opinion_pos = dash.ordinal(latest.summary.get("opinion_type"), dash.OPINIONS) if latest else None
    risk_pos = dash.ordinal(latest.summary.get("overall_risk"), dash.RISKS) if latest else None
    cards_row([
        Card("سال‌های پوشش", years_text(company.years), "date_range", "info"),
        Card("اسناد", fa_number(len(rows)), "description", "info"),
        Card("اظهارنظر", (latest.summary.get("opinion_type") or "—") if latest else "—", "gavel",
             tone(opinion_pos), help=latest_help),
        Card("سطح ریسک", (latest.summary.get("overall_risk") or "—") if latest else "—", "warning",
             tone(risk_pos), help=latest_help),
        Card("ریسک برجسته", fa_number(sum(1 for r in emphasis_rows if r["year"] == latest_year)),
             "report", "neutral", help=latest_help),
        Card("تخلفات", fa_number(sum(1 for r in legal_rows if r["year"] == latest_year)), "balance",
             "neutral", help=latest_help),
    ], key=f"dash_company_{short(key)}")

    # Charts (D-13.7)
    st.subheader(":material/trending_up: روندهای کلان", anchor=False)
    risk_points = dash.trend_points(dated, "overall_risk", dash.RISKS)
    opinion_points = dash.trend_points(dated, "opinion_type", dash.OPINIONS)
    chart_pair(
        ("روند سطح ریسک کلی", charts.trend(risk_points, dash.RISKS, "#c0392b", "سطح ریسک"), "dash_risk_trend",
         "risk_trend") if risk_points else "سطح ریسک یا سال مالی در این اسناد مشخص نیست.",
        ("روند اظهارنظر حسابرس", charts.trend(opinion_points, dash.OPINIONS, "#2980b9", "نوع اظهارنظر"),
         "dash_opinion_trend", "opinion_trend") if opinion_points else "نوع اظهارنظر یا سال مالی در این اسناد مشخص نیست.",
    )

    st.subheader(":material/report: ریسک‌های برجسته‌شده در گزارش", anchor=False)
    if emphasis_rows:
        chart_pair(
            ("روند تعداد و ترکیب ریسک‌ها",
             charts.stacked_bars(emphasis_rows, "risk_category", dash.RISK_CATEGORIES, "دسته اصلی ریسک",
                                 "تعداد ریسک‌های برجسته‌شده"), "dash_risk_bars", "risk_bars"),
            ("توزیع سلسله‌مراتبی ریسک‌ها",
             charts.sunburst(emphasis_rows, "risk_category", "risk_subcategory", "ریسک‌های برجسته",
                             dash.RISK_CATEGORIES), "dash_risk_sunburst", "risk_sunburst"),
        )
    else:
        st.info("در این گزارش‌ها ریسک برجسته‌ای (بند تأکید بر مطالب خاص) ذکر نشده است.", icon=":material/info:")

    st.subheader(":material/balance: تخلفات و الزامات قانونی", anchor=False)
    if legal_rows:
        chart_pair(
            ("روند تعداد و ترکیب تخلفات",
             charts.stacked_bars(legal_rows, "category", dash.LEGAL_CATEGORIES, "دسته اصلی تخلف",
                                 "تعداد موارد عدم رعایت"), "dash_legal_bars", "legal_bars"),
            ("ساختار و توزیع تخلفات",
             charts.sunburst(legal_rows, "category", "subcategory", "تمام تخلفات", dash.LEGAL_CATEGORIES),
             "dash_legal_sunburst", "legal_sunburst"),
        )
    else:
        st.info("در این گزارش‌ها تخلف قانونی گزارش نشده است.", icon=":material/verified:")

    st.subheader(":material/grid_on: نقشه حرارتی موضوعات کلیدی", anchor=False)
    show_all = st.toggle("نمایش همه موضوعات چک‌لیست", key="dash_heat_all",
                         help="به‌طور پیش‌فرض فقط موضوعاتی که دست‌کم در یک سال گزارش شده‌اند نمایش داده می‌شوند.")
    topics, years, matrix = dash.heatmap(dated, show_all=show_all)
    if topics and years:
        chart_block("وضعیت موضوعات کلیدی در طول زمان", charts.status_heatmap(topics, years, matrix), "dash_heatmap",
                    "heatmap")
    else:
        st.info("در چک‌لیست این اسناد موضوعی با وضعیتی غیر از «مصداق ندارد» گزارش نشده است.", icon=":material/info:")

    # Tables (D-13.9)
    st.subheader(":material/table: جدول‌ها", anchor=False)
    all_years = company.years
    picked_years = st.pills("سال", all_years, format_func=fa_digits, selection_mode="multi",
                            key=f"dash_years_{short(key)}") if all_years else []
    wanted = set(picked_years or all_years)

    def in_years(year) -> bool:
        return not picked_years or year in wanted

    tabs = st.tabs(["خلاصه سال‌ها", "ریسک‌های برجسته", "تخلفات", "چک‌لیست"])  # no basis tab (D-17.1)
    with tabs[0]:
        summary_table([r for r in rows if in_years(r.year)], with_company=len(company.member_keys) > 1)
    with tabs[1]:
        item_table([r for r in emphasis_rows if in_years(r["year"])], "emphasis")
    with tabs[2]:
        item_table([r for r in legal_rows if in_years(r["year"])], "legal")
    with tabs[3]:
        findings = [r for r in dash.items(rows, "checklist")
                    if in_years(r["year"]) and dash.ordinal(r.get("status"), dash.STATUSES) != 0]
        item_table(findings, "checklist")


# ---- Merges (D-13.5) -------------------------------------------------------------------------------
def merges_view() -> None:
    help_box("ادغام شرکت‌ها", [
        "نام‌هایی که فقط در حروف عربی/فارسی، فاصله‌ها، علائم یا عبارت‌هایی مانند «شرکت» و «(سهامی عام)» تفاوت دارند "
        "خودکار یکی در نظر گرفته می‌شوند.",
        "اگر نام یک شرکت در سال‌های مختلف واقعاً تغییر کرده است، آن‌ها را اینجا ادغام کنید؛ ادغام برای حساب شما ذخیره "
        "می‌شود و در همه منابع داده اعمال می‌شود.",
        "نام‌های نمایش‌داده‌شده همان متنی است که مدل نوشته است؛ ادغام فقط گروه‌بندی را تغییر می‌دهد.",
    ], variant="blue", icon="merge", expanded=False, key="dash_merge_help")
    names_by_key: dict[str, Counter] = {}
    docs_by_key: Counter = Counter()
    for r in records:
        names_by_key.setdefault(r.key, Counter())[r.company] += 1
        docs_by_key[r.key] += 1
    raw_names = {k: c.most_common(1)[0][0] for k, c in names_by_key.items()}

    def label(k: str) -> str:
        return f"{raw_names.get(k, k)} ({fa_number(docs_by_key.get(k, 0))} سند)"

    keys = sorted(raw_names, key=lambda k: raw_names[k])
    if len(keys) < 2:
        st.info("در این منبع داده فقط یک نام شرکت وجود دارد؛ چیزی برای ادغام نیست.", icon=":material/info:")
        keys = []
    c1, c2 = st.columns(2)
    main = c1.selectbox("نام اصلی شرکت", keys, format_func=label, key="dash_merge_main") if keys else None
    members = [] if not keys else c2.multiselect("نام‌های دیگرِ همین شرکت", [k for k in keys if k != main], format_func=label,
                             key=f"dash_merge_members_{short(main or '')}")
    if st.button("ذخیره ادغام", type="primary", icon=":material/merge:", key="dash_merge_save", disabled=not members):
        dash.save_merge(user.id, merge_map.get(main, main), members)
        st.toast("ادغام ذخیره شد.", icon=":material/merge:")
        st.rerun()

    groups: dict[str, list[str]] = {}
    for member, main_key in merge_map.items():
        groups.setdefault(main_key, []).append(member)
    st.subheader(":material/link: ادغام‌های ذخیره‌شده", anchor=False)
    if not groups:
        st.caption("هنوز ادغامی ذخیره نکرده‌اید.")
    for main_key, member_keys in sorted(groups.items(), key=lambda g: raw_names.get(g[0], g[0])):
        with st.container(border=True):
            c1, c2 = st.columns([5, 1], vertical_alignment="center")
            c1.markdown(f"**{md_escape(raw_names.get(main_key, main_key))}** ← "
                        + "، ".join(md_escape(raw_names.get(k, k)) for k in sorted(member_keys)))
            if c2.button("لغو ادغام", icon=":material/link_off:", key=f"warn_dash_unmerge_{short(main_key)}",
                         width="stretch"):
                dash.remove_group(user.id, main_key)
                st.rerun()


labels = [":material/dashboard: نمای کلی", ":material/domain: تحلیل یک شرکت"]
if user.role is not Role.GUEST:
    labels.append(":material/merge: ادغام شرکت‌ها")
tabs = st.tabs(labels)
with tabs[0]:
    overview()
with tabs[1]:
    company_view()
if user.role is not Role.GUEST:
    with tabs[2]:
        merges_view()
