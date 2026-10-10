import hashlib
import math
from datetime import timedelta

import pandas as pd
import streamlit as st

from core.fa import fa_digits, fa_number, jalali_date, jalali_datetime, parse_jalali_date, to_tehran
from core.models import Role, utcnow
from services import history
from services.jobs import REQUEST_STATUS_LABELS, get_job_manager
from services.quota import MB, quota_status
from services.users import list_users
from ui.components import Card, cards_row, confirm_button, help_box, md_escape, page_title, show_errors
from ui.request_view import show_request
from ui.session import require_user

user = require_user()
if user.role is Role.GUEST:
    st.stop()
manager = get_job_manager()

page_title("تاریخچه تحلیل‌ها", "history", "جستجو در درخواست‌ها، دریافت اکسل، اجرای مجدد، اشتراک‌گذاری و حذف")

PERIODS = {"all": "همه زمان‌ها", "7": "۷ روز اخیر", "30": "۳۰ روز اخیر", "90": "۹۰ روز اخیر", "custom": "بازه دلخواه"}
PAGE_SIZE = 20  # D-14.9


def mb(value: int) -> str:
    return fa_digits(f"{value / MB:.1f}")


def filters(prefix: str, shared: bool) -> list[history.RequestRow]:
    """Search, period, status, owner (admins) and model filters (D-12.3)."""
    c1, c2, c3 = st.columns([3, 2, 2])
    term = c1.text_input("جستجو", placeholder="شناسه درخواست، نام فایل یا نام شرکت", key=f"{prefix}_term")
    period = c2.selectbox("بازه زمانی", list(PERIODS), format_func=PERIODS.get, key=f"{prefix}_period")
    owner_id = None
    if user.is_admin and not shared:
        people = {u.id: f"{u.display_name} ({u.username})" for u in list_users()}
        owner_id = c3.selectbox("کاربر", [None, *people], format_func=lambda i: "همه کاربران" if i is None else people[i],
                                key=f"{prefix}_owner")
    since = until = None
    if period == "custom":
        d1, d2, _ = st.columns([1, 1, 2])
        since_text = d1.text_input("از تاریخ", placeholder="۱۴۰۵/۰۷/۰۱", key=f"{prefix}_from")
        until_text = d2.text_input("تا تاریخ", placeholder="۱۴۰۵/۰۷/۳۰", key=f"{prefix}_to")
        since, until = parse_jalali_date(since_text), parse_jalali_date(until_text)
        if (since_text and since is None) or (until_text and until is None):
            st.warning("تاریخ را به شکل سال/ماه/روز شمسی وارد کنید؛ مثلاً ۱۴۰۵/۰۷/۰۱.", icon=":material/event_busy:")
    elif period != "all":
        since = to_tehran(utcnow()).date() - timedelta(days=int(period) - 1)
    statuses = st.pills("وضعیت", list(history.STATUS_FILTER_LABELS), format_func=history.STATUS_FILTER_LABELS.get,
                        selection_mode="multi", key=f"{prefix}_status")
    rows = history.search_requests(user, term=term, since=since, until=until, statuses=set(statuses or []),
                                   owner_id=owner_id, shared=shared)
    models = sorted({r.model_label for r in rows})
    if len(models) > 1:
        model = st.selectbox("مدل", [None, *models], format_func=lambda m: "همه مدل‌ها" if m is None else m,
                             key=f"{prefix}_model")
        rows = [r for r in rows if model is None or r.model_label == model]
    if not rows:
        filtered = term or statuses or period != "all" or owner_id
        st.info("درخواستی با این شرایط پیدا نشد." if filtered
                else ("هنوز درخواستی با شما به اشتراک گذاشته نشده است." if shared else "هنوز درخواستی ثبت نکرده‌اید."),
                icon=":material/search_off:")
    return rows


def row_label(r: history.RequestRow, show_owner: bool) -> str:
    counts = f"{fa_number(r.total)} سند · {fa_number(r.ok)} موفق"
    if r.failed:
        counts += f" · {fa_number(r.failed)} ناموفق"
    owner = f" — {md_escape(r.username)}" if show_owner else ""
    return (f"`{r.id}`{owner} — {jalali_datetime(r.created_at)} — {md_escape(r.model_label)} — {counts} — "
            f"**{REQUEST_STATUS_LABELS.get(r.status, r.status)}**")


def requests_tab(shared: bool) -> None:
    """Requests as a list: checkbox + collapsible section per request, details load when opened (D-14.9)."""
    prefix = "hist_shared" if shared else "hist"
    rows = filters(prefix, shared)
    if not rows:
        return
    deletable = not shared and user.role is not Role.VIEWER
    show_owner = user.is_admin or shared
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(st.session_state.get(f"{prefix}_page", 0), pages - 1)
    page_rows = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    if deletable:
        selected = [r for r in rows if st.session_state.get(f"{prefix}_sel_{r.id}")]

        def select_page() -> None:
            value = st.session_state.get(f"{prefix}_sel_page_{page}", False)
            for r in page_rows:
                st.session_state[f"{prefix}_sel_{r.id}"] = value

        c1, c2 = st.columns([2, 3], vertical_alignment="center")
        c1.checkbox("انتخاب همه (این صفحه)", key=f"{prefix}_sel_page_{page}", on_change=select_page)
        if selected:
            with c2:
                running = [r for r in selected if r.running or manager.is_active(r.id)]
                prompt = (f"{fa_number(len(selected) - len(running))} درخواست و نتایج آن‌ها حذف شود؟ نتایجی که درخواست "
                          "دیگری از آن‌ها استفاده می‌کند باقی می‌مانند. سهمیه مصرف‌شده برنمی‌گردد. این کار برگشت‌پذیر نیست.")
                if running:
                    prompt += f"\n\n{fa_number(len(running))} درخواست در حال اجرا حذف نمی‌شود: " + "، ".join(
                        f"`{r.id}`" for r in running)
                if confirm_button(f"حذف {fa_number(len(selected))} درخواست انتخاب‌شده", key=f"{prefix}_bulk_delete",
                                  prompt=prompt):
                    deleted, errors = 0, []
                    for r in selected:
                        if r in running:
                            continue
                        try:
                            history.delete_request(user, r.id, running=manager.is_active(r.id))
                            manager.forget(r.id)
                            st.session_state.pop(f"{prefix}_sel_{r.id}", None)
                            deleted += 1
                        except history.HistoryError as exc:
                            errors.append(f"{r.id}: {exc}")
                    st.toast(f"{fa_number(deleted)} درخواست حذف شد.", icon=":material/delete:")
                    if errors:
                        show_errors(history.HistoryError("\n".join(errors)))
                    else:
                        st.rerun()

    for r in page_rows:
        if deletable:
            c1, c2 = st.columns([1, 24], vertical_alignment="top")
            c1.checkbox("انتخاب", key=f"{prefix}_sel_{r.id}", label_visibility="collapsed")
            target = c2
        else:
            target = st.container()
        section = target.expander(row_label(r, show_owner), key=f"{prefix}_exp_{r.id}", on_change="rerun",
                                  icon=":material/description:")
        if section.open:
            with section:
                show_request(r.id, user, key=prefix,
                             on_deleted=lambda rid=r.id: st.session_state.pop(f"{prefix}_exp_{rid}", None))

    if pages > 1:
        c1, c2, c3 = st.columns([1, 2, 1], vertical_alignment="center")
        if c1.button("صفحه قبل", icon=":material/chevron_right:", key=f"{prefix}_prev", disabled=page == 0,
                     width="stretch"):
            st.session_state[f"{prefix}_page"] = page - 1
            st.rerun()
        c2.caption(f"صفحه {fa_number(page + 1)} از {fa_number(pages)} — {fa_number(len(rows))} درخواست")
        if c3.button("صفحه بعد", icon=":material/chevron_left:", key=f"{prefix}_next", disabled=page >= pages - 1,
                     width="stretch"):
            st.session_state[f"{prefix}_page"] = page + 1
            st.rerun()
    else:
        st.caption(f"{fa_number(len(rows))} درخواست — برای مشاهده جزئیات، روی هر ردیف کلیک کنید.")


def kept_files_tab() -> None:
    """The user's kept PDFs (admins: everyone's): download and delete selected (D-12.4, D-14.10)."""
    files = history.kept_files(user)
    own_total = sum(f.size_bytes for f in files if f.user_id == user.id)
    cards = [Card("فایل‌های نگه‌داری‌شده", fa_number(len(files)), "picture_as_pdf", "info"),
             Card("حجم کل", f"{mb(sum(f.size_bytes for f in files))} مگابایت", "cloud", "neutral")]
    if user.role is Role.ANALYST:
        quota = quota_status(user.id)
        cards.append(Card("سهمیه فایل‌ها", f"{mb(own_total)} از {mb(quota.storage_quota_bytes or 0)} مگابایت",
                          "data_usage", "warning" if quota.storage_room() == 0 else "info"))
    cards_row(cards, key="hist_files")
    help_box("درباره فایل‌های نگه‌داری‌شده", [
        "فایل‌های PDF برای تلاش مجدد و مراجعه بعدی نگه‌داری می‌شوند و از سهمیه فایل شما کم می‌کنند.",
        "حذف فایل، نتایج تحلیل را حذف نمی‌کند؛ فقط برای اجرای مجدد آن سند باید فایل را دوباره بارگذاری کنید.",
        "برای دانلود یا حذف، یک یا چند ردیف را انتخاب کنید؛ چند فایل به‌صورت یک فایل ZIP دانلود می‌شوند.",
    ], variant="blue", icon="info", expanded=False, key="hist_files_help")
    if not files:
        st.info("فایلی نگه‌داری نشده است.", icon=":material/info:")
        return
    table = pd.DataFrame([{
        "فایل": f.file_name,
        **({"کاربر": f.username} if user.is_admin else {}),
        "حجم (مگابایت)": mb(f.size_bytes),
        "تاریخ": jalali_date(f.created_at),
        "تعداد درخواست": fa_number(f.requests),
    } for f in files])
    signature = hashlib.md5("|".join(f"{f.user_id}:{f.document_id}" for f in files).encode()).hexdigest()[:8]
    event = st.dataframe(table[table.columns[::-1]], hide_index=True, on_select="rerun", selection_mode="multi-row",
                         key=f"hist_files_table_{signature}")
    chosen = [files[i] for i in (event.selection.rows if event else []) if i < len(files)]
    if not chosen:
        st.caption("برای دانلود یا حذف، یک یا چند ردیف را انتخاب کنید.")
        return
    items = [(f.user_id, f.document_id, f.file_name) for f in chosen]
    c1, c2, _ = st.columns([1, 1, 2])
    single = len(chosen) == 1
    c1.download_button(
        f"دانلود {fa_number(len(chosen))} فایل" + ("" if single else " (ZIP)"),
        lambda: history.kept_pdfs(user, items)[0],
        file_name=chosen[0].file_name if single else "kept_files.zip",
        mime="application/pdf" if single else "application/zip", icon=":material/download:", type="primary",
        on_click="ignore", key=f"hist_files_download_{signature}", width="stretch",
    )
    with c2:
        prompt = (f"{fa_number(len(chosen))} فایل ({mb(sum(f.size_bytes for f in chosen))} مگابایت) دیگر نگه‌داری نشود؟ "
                  "نتایج تحلیل باقی می‌مانند.")
        if confirm_button(f"حذف {fa_number(len(chosen))} فایل", key="hist_files_delete", prompt=prompt):
            try:
                history.delete_kept_files(user, [(f.user_id, f.document_id) for f in chosen])
                st.toast("فایل‌ها حذف شدند.", icon=":material/delete:")
                st.rerun()
            except history.HistoryError as exc:
                show_errors(exc)


if user.role is Role.VIEWER:
    st.caption(":material/share: درخواست‌هایی که با شما به اشتراک گذاشته شده‌اند (فقط مشاهده).")
    requests_tab(shared=True)
else:
    labels = [":material/list: " + ("همه درخواست‌ها" if user.is_admin else "درخواست‌های من")]
    if user.role is Role.ANALYST:  # D-14.7
        labels.append(":material/share: اشتراک‌گذاشته با من")
    labels.append(":material/picture_as_pdf: فایل‌های نگه‌داری‌شده")
    tabs = st.tabs(labels)
    with tabs[0]:
        requests_tab(shared=False)
    if user.role is Role.ANALYST:
        with tabs[1]:
            requests_tab(shared=True)
    with tabs[-1]:
        kept_files_tab()
