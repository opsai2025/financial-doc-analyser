"""One request's view, shared by the analysis and history pages (D-12.2, D-14).

Status cards and the document table refresh live while the request runs. The actions sit outside
the refreshing part (D-14.1): Excel, dashboard, sharing and raw output are available during the run,
plus «توقف» (D-14.2); re-run, re-upload, input PDFs and deletion once it is finished or stopped.
What is shown depends on the role (shared users: read-only, D-12.7, D-14.7).
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd
import streamlit as st

from core.db import session_scope
from core.fa import fa_digits, fa_number, jalali_datetime, to_tehran
from core.models import Role
from services import app_settings, history, results
from services import providers as pv
from services.auth import CurrentUser
from services.dashboard import merges
from services.excel import build_workbook, workbook_name
from services.report import build_report, report_name
from services.jobs import (
    DUPLICATE,
    FAILED,
    INTERRUPTED,
    PROCESSING,
    QUEUED,
    REQUEST_STATUS_LABELS,
    RETRYABLE_STATUSES,
    REUSED,
    SKIPPED_QUOTA,
    STOPPED,
    STOPPING,
    SUCCEEDED,
    JobError,
    RequestState,
    doc_explanation,
    doc_status_label,
    get_job_manager,
)
from services.uploads import from_pdfs
from ui.components import Card, cards_row, confirm_button, file_label, md_escape, show_errors

# Status groups of the re-run filter (D-14.3); duplicates are never selectable
RERUN_GROUPS = {
    "failed": ("ناموفق", {FAILED}),
    "stopped": ("متوقف‌شده", {STOPPED, INTERRUPTED}),
    "skipped": ("رد شده – سهمیه", {SKIPPED_QUOTA}),
    "succeeded": ("موفق", {SUCCEEDED}),
    "reused": ("از نتایج قبلی", {REUSED}),
}
UNFINISHED_GROUPS = ("failed", "stopped", "skipped")


DOC_COLUMN_WIDTHS = {"فایل": 120, "وضعیت": 125, "تلاش": 45, "مدل": 110, "شرکت": 120, "سال": 45, "اظهارنظر": 80,
                     "سطح ریسک": 70, "زمان (آخرین / کل)": 90, "توکن (کل)": 70, "توضیح": 120}


def _seconds(value: float | None) -> str:
    return "—" if not value else f"{fa_digits(round(value))} ث"


def _doc_rows(state: RequestState) -> list[dict]:
    mixed = any(d.source_model for d in state.docs)
    rows = []
    for doc in state.docs:
        summary = doc.summary or {}
        row = {
            "فایل": doc.file_name,
            "وضعیت": doc_status_label(doc),
            "تلاش": fa_number(doc.attempts) if doc.attempts else "—",
        }
        if mixed:  # results reused from other models (D-12.1)
            row["مدل"] = doc.source_model or state.model_label
        row |= {
            "شرکت": summary.get("company_name") or "",
            "سال": fa_digits(summary["year"]) if summary.get("year") else "",
            "اظهارنظر": summary.get("opinion_type") or "",
            "سطح ریسک": summary.get("overall_risk") or "",
            # D-14.5 / D-14.6: model time (last attempt / all attempts) and tokens of all attempts
            "زمان (آخرین / کل)": (f"{_seconds(doc.last_call_seconds)} / {_seconds(doc.total_call_seconds)}"
                                  if doc.total_call_seconds else "—"),
            "توکن (کل)": fa_number(doc.total_input_tokens + doc.total_output_tokens)
            if doc.total_input_tokens or doc.total_output_tokens else "—",
            "توضیح": doc_explanation(doc),  # also says when a result could not be stored
        }
        rows.append(row)
    return rows


def _status(state: RequestState, user: CurrentUser) -> None:
    total = len(state.docs)
    done = sum(1 for d in state.docs if d.status not in (QUEUED, PROCESSING))
    owner = f" — **کاربر:** `{state.username}`" if state.username and state.user_id != user.id else ""
    parent = f" — **اجرای مجدد از:** `{state.parent_request_id}`" if state.parent_request_id else ""
    st.markdown(f"**شناسه درخواست:** `{state.id}`{owner}{parent} — **مدل:** {md_escape(state.model_label)} — "
                f"**زمان:** {jalali_datetime(state.created_at)} — "
                f"**وضعیت:** {REQUEST_STATUS_LABELS.get(state.status, state.status)}")
    if state.status == STOPPING:
        st.caption(f":material/pause_circle: در حال توقف — {fa_number(state.count(PROCESSING))} تحلیل در حال اجرا "
                   "به پایان می‌رسد و نتیجه‌اش نگه‌داری می‌شود؛ تحلیل جدیدی شروع نمی‌شود.")
    elif state.is_running:  # the host stops an idle server: no open tab -> unfinished documents are interrupted
        st.caption(":material/tab: تا پایان تحلیل این صفحه را باز نگه دارید. اگر همه صفحه‌های برنامه بسته شوند، "
                   "ممکن است سرور متوقف شود و اسناد ناتمام «متوقف شده» ثبت شوند؛ با «ادامه یا اجرای مجدد» ادامه می‌یابند.")
    failed = state.count(FAILED, INTERRUPTED, STOPPED)
    cards_row([
        Card("کل اسناد", fa_number(total), "inventory_2", "info"),
        Card("موفق", fa_number(state.count(SUCCEEDED, REUSED)), "check_circle", "success"),
        Card("ناموفق / متوقف", fa_number(failed), "error", "danger" if failed else "neutral"),
        Card("در صف / در حال تحلیل", fa_number(state.count(QUEUED, PROCESSING)), "pending", "warning"),
        Card("تکراری / رد شده", fa_number(state.count(DUPLICATE, SKIPPED_QUOTA)), "block", "neutral"),
    ], key=f"req_{state.id}")
    st.progress(done / total if total else 1.0,
                text=f"{fa_number(done)} از {fa_number(total)} سند به وضعیت نهایی رسیده است")
    table = pd.DataFrame(_doc_rows(state))
    # The grid is left-to-right and shows its left end first: fixed widths keep the (right-hand) file and
    # status columns on screen; long explanations are cut and shown in full on hover (D-9.9).
    st.dataframe(table[table.columns[::-1]], hide_index=True, column_config={
        name: st.column_config.TextColumn(width=width) for name, width in DOC_COLUMN_WIDTHS.items()})


def show_request(request_id: str, user: CurrentUser, *, key: str, on_deleted: Callable[[], None] | None = None) -> None:
    """Live status while running; the actions below it. `key` keeps widget keys unique per page."""
    manager = get_job_manager()
    state = manager.get(request_id, user)
    if state is None:
        st.info("این درخواست دیگر در دسترس نیست.", icon=":material/info:")
        return
    poll_key = f"{key}_polling_{request_id}"
    if state.is_running:
        st.session_state[poll_key] = True

        @st.fragment(run_every=2)
        def live() -> None:
            current = manager.get(request_id, user)
            if current is None:
                return
            _status(current, user)
            if not current.is_running and st.session_state.pop(poll_key, False):
                st.rerun()  # once final, redraw the whole page with all actions

        live()
    else:
        st.session_state.pop(poll_key, None)
        _status(state, user)
    _actions(state, user, key, on_deleted)


def _actions(state: RequestState, user: CurrentUser, key: str, on_deleted: Callable[[], None] | None) -> None:
    manager = get_job_manager()
    is_owner = state.user_id == user.id
    may_manage = state.persist and (is_owner or user.is_admin)
    running = state.is_running

    if not state.persist and (expires := manager.expires_at(state.id)):
        st.caption(f":material/schedule: نتایج این درخواست ذخیره نمی‌شوند و تا ساعت "
                   f"{fa_digits(to_tehran(expires).strftime('%H:%M'))} (وقت تهران) در دسترس هستند.")

    c1, c4, c2, c3 = st.columns([1, 1, 1, 1])
    c1.download_button(
        "دانلود اکسل", lambda: build_workbook(manager.get(state.id, user) or state), file_name=workbook_name(state),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary",
        icon=":material/download:", key=f"{key}_excel_{state.id}", on_click="ignore", width="stretch",
        help="در حین اجرا، اسناد تمام‌شده با نتیجه و بقیه فقط در برگه وضعیت می‌آیند." if running else None,
    )
    c4.download_button(  # D-16.1: same rows as the Excel, by company with the user's merges (D-17.5)
        "گزارش Word", lambda: build_report(manager.get(state.id, user) or state, merges(user.id)),
        file_name=report_name(state),
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", type="primary",
        icon=":material/description:", key=f"{key}_word_{state.id}", on_click="ignore", width="stretch",
        help="در حین اجرا، فقط اسناد تمام‌شده در گزارش می‌آیند." if running else None,
    )
    if any(d.status in (SUCCEEDED, REUSED) for d in state.docs) and c2.button(
            "نمایش در داشبورد", icon=":material/monitoring:", key=f"{key}_dash_{state.id}", width="stretch"):
        st.session_state.dash_open = state.id  # preselects this request as the dashboard's source (D-13.1)
        st.switch_page("app_pages/dashboard.py")
    if running and (is_owner or user.is_admin) and state.status != STOPPING and c3.button(
            "توقف درخواست", icon=":material/stop_circle:", key=f"warn_{key}_stop_{state.id}", width="stretch",
            help="اسنادی که هنوز به مدل ارسال نشده‌اند متوقف می‌شوند؛ تحلیل‌های در حال اجرا تمام و نگه‌داری می‌شوند."):
        try:
            manager.stop(state.id, user)
            st.toast("درخواست در حال توقف است.", icon=":material/stop_circle:")
            st.rerun()
        except JobError as exc:
            show_errors(exc)

    if not running and is_owner:
        _rerun_panel(state, user, key)
        if missing := manager.missing_files(state, all_docs=True):
            _reupload(state, user, key, missing)
    if may_manage:
        _sharing(state, user, key)
    if user.role is not Role.VIEWER:
        _raw_output(state, key)
    if may_manage and not running:
        _input_files(state, key)
        prompt = (f"درخواست `{state.id}` و نتایج آن حذف شود؟ نتایجی که درخواست دیگری از آن‌ها استفاده می‌کند "
                  "تا حذف آن درخواست باقی می‌مانند. سهمیه مصرف‌شده برنمی‌گردد. این کار برگشت‌پذیر نیست.")
        if confirm_button("حذف درخواست", key=f"{key}_delete_{state.id}", prompt=prompt):
            try:
                history.delete_request(user, state.id, running=manager.is_active(state.id))
                manager.forget(state.id)
                st.toast("درخواست حذف شد.", icon=":material/delete:")
                if on_deleted:
                    on_deleted()
                st.rerun()
            except history.HistoryError as exc:
                show_errors(exc)


def _rerun_panel(state: RequestState, user: CurrentUser, key: str) -> None:
    """Resume unfinished documents or re-run any of them, chosen by status (D-14.3, D-14.4)."""
    manager = get_job_manager()
    selectable = [d for d in state.docs if d.status != DUPLICATE]
    group_of = {status: g for g, (_, statuses) in RERUN_GROUPS.items() for status in statuses}
    counts = {g: sum(1 for d in selectable if group_of.get(d.status) == g) for g in RERUN_GROUPS}
    present = [g for g in RERUN_GROUPS if counts[g]]
    unfinished = sum(counts[g] for g in UNFINISHED_GROUPS)
    title = (f"ادامه یا اجرای مجدد ({fa_number(unfinished)} سند ناتمام)" if unfinished
             else "اجرای مجدد اسناد")
    panel = st.expander(f":material/replay: {title}", key=f"{key}_rerun_exp_{state.id}", on_change="rerun")
    if not panel.open:
        return
    with panel:
        choices = pv.usable_models(user.id)
        if not choices:
            st.info("هیچ مدلی برای تحلیل در دسترس شما نیست.", icon=":material/info:")
            return
        groups = st.pills("نمایش اسناد", present, default=[g for g in present if g in UNFINISHED_GROUPS] or present,
                          selection_mode="multi", key=f"{key}_rerun_groups_{state.id}",
                          format_func=lambda g: f"{RERUN_GROUPS[g][0]} ({fa_number(counts[g])})")
        shown = [d for d in selectable if group_of.get(d.status) in set(groups or [])]
        if not shown:
            st.caption("وضعیتی برای نمایش انتخاب نشده است.")
            return
        table = pd.DataFrame([{"انتخاب": True, "فایل": d.file_name, "وضعیت": doc_status_label(d),
                               "تلاش": fa_number(d.attempts) if d.attempts else "—",
                               "توضیح": doc_explanation(d)} for d in shown])
        signature = "_".join(sorted(groups or []))
        edited = st.data_editor(table[table.columns[::-1]], hide_index=True, disabled=["فایل", "وضعیت", "تلاش", "توضیح"],
                                key=f"{key}_rerun_table_{state.id}_{signature}",
                                column_config={"انتخاب": st.column_config.CheckboxColumn(width="small"),
                                               "توضیح": st.column_config.TextColumn(width="medium")})
        picked = [d for d, on in zip(shown, edited["انتخاب"], strict=True) if on]

        current = next((c for c in choices if c.model_id == state.model_id), None)
        c1, c2 = st.columns([3, 2])
        choice = c1.selectbox("مدل", choices, index=choices.index(current) if current else 0, format_func=lambda c: c.label,
                              key=f"{key}_rerun_model_{state.id}")
        same_model = choice.model_id == state.model_id
        default_keys = [k.id for k in choice.keys if not same_model or k.id in state.key_ids] or [k.id for k in choice.keys]
        key_ids = c2.multiselect("کلیدهای API", [k.id for k in choice.keys], default=default_keys,
                                 format_func={k.id: f"{k.label} (••••{k.last4})" for k in choice.keys}.get,
                                 key=f"{key}_rerun_keys_{state.id}_{choice.model_id}")
        resume = [d for d in picked if same_model and d.status in RETRYABLE_STATUSES]
        fresh = [d for d in picked if d not in resume]
        parts = []
        if resume:
            parts.append(f"{fa_number(len(resume))} سند در همین درخواست ادامه می‌یابد")
        if fresh:
            parts.append(f"{fa_number(len(fresh))} سند در یک درخواست جدیدِ مرتبط با این درخواست تحلیل می‌شود"
                         + ("" if same_model else " (مدل دیگری انتخاب شده است)"))
        st.caption("؛ ".join(parts) + "." if parts else "سندی انتخاب نشده است.")
        if st.button(f"اجرای {fa_number(len(picked))} سند", type="primary", icon=":material/play_arrow:",
                     key=f"{key}_rerun_go_{state.id}", disabled=not picked or not key_ids):
            try:
                resumed, new_id = manager.rerun(state.id, user, [d.position for d in picked], choice, key_ids)
            except JobError as exc:
                show_errors(exc)
                return
            if new_id:
                st.session_state[f"{key}_rerun_new"] = new_id
            st.toast(f"{fa_number(resumed + len(fresh))} سند در صف تحلیل قرار گرفت.", icon=":material/play_arrow:")
            st.rerun()
        if new_id := st.session_state.get(f"{key}_rerun_new"):
            st.success(f"درخواست جدید `{new_id}` ساخته شد؛ آن را در «تاریخچه تحلیل‌ها» ببینید.",
                       icon=":material/fiber_new:")


def _reupload(state: RequestState, user: CurrentUser, key: str, missing) -> None:
    """PDFs no longer available, matched by content when uploaded again (D-12.15)."""
    needed = any(d.status in RETRYABLE_STATUSES for d in missing)
    with st.expander(f":material/upload_file: فایل {fa_number(len(missing))} سند در دسترس نیست "
                     "(برای ادامه یا اجرای مجدد لازم است)", expanded=needed):
        st.markdown("\n".join(f"- {md_escape(file_label(d.file_name))} — {doc_status_label(d)}" for d in missing))
        st.caption("همان فایل‌ها را دوباره بارگذاری کنید؛ فایل‌ها بر اساس محتوا شناسایی می‌شوند و نام فایل مهم نیست.")
        nonce = st.session_state.setdefault(f"{key}_reup_nonce", 0)
        uploads = st.file_uploader("فایل‌های PDF", type=["pdf"], accept_multiple_files=True,
                                   key=f"{key}_reup_{state.id}_{nonce}",
                                   max_upload_size=app_settings.get("max_pdf_mb"))
        if uploads and st.button("پیوست فایل‌ها", icon=":material/attach_file:", key=f"{key}_reup_go_{state.id}"):
            intake = from_pdfs([(f.name, f.getvalue()) for f in uploads])
            try:
                matched, rejected = get_job_manager().provide_files(state, user, intake.files)
            except JobError as exc:
                show_errors(exc)
                return
            st.session_state[f"{key}_reup_nonce"] = nonce + 1
            notes = [*intake.rejected, *(f"«{file_label(name)}»: با هیچ‌یک از فایل‌های موردنیاز یکسان نیست."
                                         for name in rejected)]
            st.session_state[f"{key}_reup_msg"] = (len(matched), notes)
            st.rerun()
        if message := st.session_state.pop(f"{key}_reup_msg", None):
            count, notes = message
            if count:
                st.success(f"{fa_number(count)} فایل پیوست شد؛ اکنون از «ادامه یا اجرای مجدد» استفاده کنید.",
                           icon=":material/check:")
            if notes:
                st.warning("\n".join(f"- {md_escape(n)}" for n in notes), icon=":material/warning:")


def _input_files(state: RequestState, key: str) -> None:
    """Download the uploaded PDFs (owner and admins, D-14.10)."""
    panel = st.expander(":material/picture_as_pdf: دانلود فایل‌های ورودی", key=f"{key}_pdfs_exp_{state.id}",
                        on_change="rerun")
    if not panel.open:
        return
    manager = get_job_manager()
    with panel:
        available = manager.pdf_available(state)
        for doc in (d for d in state.docs if d.status != DUPLICATE):
            c1, c2 = st.columns([4, 1], vertical_alignment="center")
            c1.markdown(md_escape(file_label(doc.file_name)))
            if doc.document_id in available:
                c2.download_button("دانلود", lambda d=doc: manager.pdf_bytes(state, d) or b"", file_name=doc.file_name,
                                   mime="application/pdf", icon=":material/download:", on_click="ignore",
                                   key=f"{key}_pdf_{state.id}_{doc.position}", width="stretch")
            else:
                c2.caption("در دسترس نیست")
        st.caption("فایل‌هایی که نگه‌داری نشده‌اند (فضای فایل پر بود یا حذف شده‌اند) قابل دانلود نیستند.")


def _sharing(state: RequestState, user: CurrentUser, key: str) -> None:
    """Share with analysts and viewers (D-12.6, D-14.7)."""
    options = history.share_options(state.user_id)
    current = history.shared_with(state.id)
    label = f"اشتراک‌گذاری ({fa_number(len(current))} نفر)" if current else "اشتراک‌گذاری"
    with st.expander(f":material/share: {label}"):
        if not options:
            st.caption("هیچ حساب فعال تحلیلگر یا مشاهده‌گری وجود ندارد.")
            return
        role_names = {Role.ANALYST: "تحلیلگر", Role.VIEWER: "مشاهده‌گر"}
        names = {o.id: f"{o.display_name} ({o.username}) — {role_names[o.role]}" for o in options}
        chosen = st.multiselect("کاربران", list(names), default=[i for i in current if i in names],
                                format_func=names.get, key=f"{key}_share_{state.id}")
        st.caption("کاربرانی که درخواست با آن‌ها به اشتراک گذاشته شده فقط آن را می‌بینند: وضعیت اسناد، جدول نتایج، "
                   "اکسل و داشبورد (تحلیلگر خروجی خام JSON را هم می‌بیند). اجرای مجدد، حذف و دانلود PDF ممکن نیست.")
        if st.button("ذخیره اشتراک‌گذاری", icon=":material/save:", key=f"{key}_share_save_{state.id}"):
            try:
                history.set_shares(user, state.id, chosen)
                st.toast("اشتراک‌گذاری ذخیره شد.", icon=":material/share:")
                st.rerun()
            except history.HistoryError as exc:
                show_errors(exc)


def _raw_output(state: RequestState, key: str) -> None:
    manager = get_job_manager()
    with_results = [d for d in state.docs if d.status in (SUCCEEDED, REUSED)]
    if with_results:
        with st.expander(":material/data_object: مشاهده خروجی خام مدل (JSON)"):
            doc = st.selectbox("سند", with_results, format_func=lambda d: file_label(d.file_name),
                               key=f"{key}_json_{state.id}")
            data = doc.data
            if data is None and doc.result_request_id:
                with session_scope() as db:
                    data = results.load_json(db, doc.result_request_id, doc.document_id)
            st.json(data or {}, expanded=False)

    flagged = [d for d in state.docs if d.warnings or d.raw_answers]
    if flagged:
        with st.expander(":material/warning: پاسخ‌های نگه‌داری‌شده مدل (پاسخ‌هایی که JSON تمیز نبودند)"):
            st.caption("متن کامل پاسخ مدل، همان‌طور که دریافت شد. «پذیرفته‌شده»: JSON اول آن استفاده شد؛ "
                       "«ردشده»: قابل استفاده نبود.")
            doc = st.selectbox("سند", flagged, format_func=lambda d: file_label(d.file_name),
                               key=f"{key}_raw_{state.id}")
            answers = manager.raw_answers(state, doc)
            if not answers:
                st.info("برای این سند پاسخی نگه‌داری نشده است.", icon=":material/info:")
            for index, answer in enumerate(answers):
                kind = "پذیرفته‌شده با هشدار" if answer["kind"] == "accepted" else "ردشده"
                st.download_button(
                    f"تلاش {fa_digits(answer['attempt'])} — {kind} — {fa_number(len(answer['text']))} نویسه",
                    answer["text"].encode("utf-8"), file_name=f"{state.id}_{doc.position + 1}_attempt{answer['attempt']}.txt",
                    mime="text/plain", icon=":material/download:", key=f"{key}_rawdl_{state.id}_{doc.position}_{index}",
                    on_click="ignore",
                )
