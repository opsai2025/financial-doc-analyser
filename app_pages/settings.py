import pandas as pd
import streamlit as st

from core.config import MAX_PARALLEL_CALLS_RANGE
from core.fa import fa_digits, fa_number
from core.models import Role
from services import app_settings
from services import providers as pv
from services.jobs import get_job_manager
from services.users import ROLE_LABELS, list_users
from ui.components import Card, cards_row, help_box, md_escape, page_title, show_errors
from ui.provider_editor import provider_help, provider_manager
from ui.session import require_user

admin = require_user()
if not admin.is_admin:
    st.stop()

page_title("تنظیمات سامانه", "settings", "ارائه‌دهندگان، مدل‌ها و کلیدهای API مشترک و دسترسی کاربران به آن‌ها")

tab_providers, tab_access, tab_engine, tab_defaults = st.tabs([
    ":material/hub: ارائه‌دهندگان، مدل‌ها و کلیدها", ":material/admin_panel_settings: دسترسی‌ها",
    ":material/speed: وضعیت موتور", ":material/tune: مقادیر پیش‌فرض",
])

with tab_providers:
    provider_help(own=False)
    provider_manager(None, prefix="admin_prov")

MODE_LABELS = {"none": "بدون دسترسی", "all": "همه مدل‌ها و کلیدها", "custom": "انتخابی"}
OVERRIDE_LABELS = {"none": "ندارد", "all": "همه مدل‌ها و کلیدها", "custom": "انتخابی"}


def grant_mode(grant) -> str:
    if grant is None:
        return "none"
    return "all" if grant.model_ids is None and grant.api_key_ids is None else "custom"


def grant_editor(subject: pv.Subject, provider, effect: str, labels: dict, key: str) -> None:
    models = pv.list_models(provider.id)
    keys = pv.list_keys(provider.id)
    grant = pv.get_grant(subject, provider.id, effect)
    mode = st.segmented_control(
        "دسترسی" if effect == "allow" else "محرومیت", list(labels), default=grant_mode(grant),
        format_func=labels.get, key=f"{key}_mode",
    ) or "none"
    model_ids = key_ids = None
    if mode == "custom":
        c1, c2 = st.columns(2)
        by_model = {m.id: m for m in models}
        by_key = {k.id: k for k in keys}
        model_ids = c1.multiselect(
            "مدل‌ها", list(by_model), default=[i for i in (grant.model_ids or []) if i in by_model] if grant else [],
            format_func=lambda i: by_model[i].display_name, key=f"{key}_models", placeholder="خالی = همه مدل‌ها",
        ) or None
        key_ids = c2.multiselect(
            "کلیدها", list(by_key), default=[i for i in (grant.api_key_ids or []) if i in by_key] if grant else [],
            format_func=lambda i: f"{by_key[i].label} (••••{by_key[i].key_last4})", key=f"{key}_keys",
            placeholder="خالی = همه کلیدها",
        ) or None
    if st.button("ذخیره", key=f"{key}_save", icon=":material/save:", type="primary"):
        try:
            if mode == "none":
                pv.clear_grant(subject, provider.id, effect)
            else:
                pv.set_grant(subject, provider.id, effect, model_ids, key_ids)
            st.toast("دسترسی ذخیره شد.", icon=":material/check_circle:")
            st.rerun()
        except pv.ProviderError as exc:
            show_errors(exc)


with tab_access:
    help_box(
        "دسترسی‌ها چگونه تعیین می‌شوند؟",
        [
            "**پیش‌فرض نقش:** همه تحلیلگران (یا همه مهمان‌ها) به مدل‌ها و کلیدهای انتخاب‌شده دسترسی دارند.",
            "**استثنای کاربر:** برای یک کاربر می‌توانید دسترسی اضافه بدهید یا بخشی را محروم کنید؛ محرومیت بر دسترسی مقدم است.",
            "مدیر سامانه همیشه به همه ارائه‌دهندگان دسترسی دارد؛ مشاهده‌گر امکان تحلیل ندارد.",
            "ارائه‌دهندگان شخصی کاربران فقط برای خودشان قابل استفاده‌اند و اینجا نمایش داده نمی‌شوند.",
        ],
        variant="blue", icon="help", expanded=False, key="access_help",
    )
    providers = pv.list_providers(None)
    if not providers:
        st.info("ابتدا در زبانه «ارائه‌دهندگان» یک ارائه‌دهنده تعریف کنید.", icon=":material/info:")
    else:
        subject_type = st.segmented_control(
            "نوع تنظیم", ["role", "user"], default="role", key="access_subject_type",
            format_func={"role": ":material/groups: پیش‌فرض نقش", "user": ":material/person: استثنای کاربر"}.get,
        ) or "role"
        if subject_type == "role":
            role = st.selectbox("نقش", [Role.ANALYST, Role.GUEST], format_func=ROLE_LABELS.get, key="access_role")
            subject, target_user = pv.Subject(role=role), None
        else:
            candidates = [u for u in list_users() if u.role in (Role.ANALYST, Role.GUEST)]
            target_user = st.selectbox(
                "کاربر", candidates, key="access_user", index=0 if candidates else None,
                format_func=lambda u: f"{u.display_name} ({u.username}) — {ROLE_LABELS[Role(u.role)]}",
                placeholder="کاربر تحلیلگر یا مهمانی وجود ندارد",
            )
            subject = pv.Subject(user_id=target_user.id) if target_user else None

        for provider in providers if subject else []:
            with st.container(border=True):
                st.markdown(f"**:material/hub: {md_escape(provider.name)}**"
                            + ("" if provider.is_active else " — غیرفعال"))
                base = f"access_{subject_type}_{subject.role or subject.user_id}_{provider.id}"
                if subject_type == "role":
                    grant_editor(subject, provider, "allow", MODE_LABELS, base)
                else:
                    st.caption("دسترسی اضافه (علاوه بر پیش‌فرض نقش)")
                    grant_editor(subject, provider, "allow", OVERRIDE_LABELS, f"{base}_allow")
                    st.caption("محرومیت (مقدم بر دسترسی)")
                    grant_editor(subject, provider, "deny", OVERRIDE_LABELS, f"{base}_deny")

        if target_user is not None:
            st.subheader(":material/fact_check: دسترسی نهایی این کاربر", anchor=False)
            effective = [c for c in pv.usable_models(target_user.id) if not c.own]
            if not effective:
                st.warning("این کاربر به هیچ مدل مشترکی دسترسی ندارد.", icon=":material/block:")
            for choice in effective:
                keys = "، ".join(f"{k.label} (••••{k.last4})" for k in choice.keys)
                st.markdown(f"- **{md_escape(choice.label)}** — کلیدها: {md_escape(keys)}")



def _limit_text(used: int, limit: int | None) -> str:
    return f"{fa_number(used)} از {fa_number(limit)}" if limit else f"{fa_number(used)} (نامحدود)"


@st.fragment(run_every=3)
def engine_status() -> None:
    """Live view of the scheduler (D-11.14). Counters since the server started; "today" from the database."""
    view = get_job_manager().scheduler.snapshot()
    queued = sum(r.waiting + r.retry_waiting for r in view.runs)
    cooling = sum(1 for s in view.slots if s.cooldown_s > 0)
    cards_row([
        Card("در حال اجرا / سقف سرور", f"{fa_number(view.running)} از {fa_number(view.cap)}", "bolt",
             "warning" if view.running >= view.cap else "info", help="سقف کل سرور در زبانه «مقادیر پیش‌فرض»"),
        Card("درخواست‌های فعال", fa_number(len(view.runs)), "pending_actions", "info"),
        Card("اسناد در صف", fa_number(queued), "hourglass_top", "warning" if queued else "neutral"),
        Card("کلیدهای در وقفه ۴۲۹", fa_number(cooling), "pause_circle", "danger" if cooling else "neutral"),
    ], key="engine")

    st.subheader(":material/key: کلیدها", anchor=False)
    if view.slots:
        table = pd.DataFrame([{
            "کلید": s.key_label,
            "مدل": s.model_label,
            "در حال اجرا": f"{fa_number(s.running)} از {fa_number(s.concurrency)}",
            "شروع در یک دقیقه اخیر": _limit_text(s.starts_last_minute, s.rpm),
            "درخواست امروز (UTC)": _limit_text(s.used_today, s.rpd),
            "وقفه": f"{fa_digits(int(s.cooldown_s) + 1)} ثانیه" if s.cooldown_s > 0 else "—",
            "وضعیت": "فعال" if s.active else "غیرفعال",
        } for s in view.slots])
        st.dataframe(table[table.columns[::-1]], hide_index=True)
    else:
        st.caption("از زمان راه‌اندازی سرور هنوز تحلیلی اجرا نشده است.")

    st.subheader(":material/list_alt: درخواست‌های در حال اجرا", anchor=False)
    if view.runs:
        table = pd.DataFrame([{
            "شناسه": r.request_id, "کاربر": r.username, "مدل": r.model_label,
            "در حال تحلیل": fa_number(r.running), "در صف": fa_number(r.waiting),
            "در انتظار تلاش مجدد": fa_number(r.retry_waiting), "باقی‌مانده": fa_number(r.pending),
        } for r in view.runs])
        st.dataframe(table[table.columns[::-1]], hide_index=True)
    else:
        st.caption("در حال حاضر درخواستی در صف یا در حال اجرا نیست.")


with tab_engine:
    help_box(
        "این صفحه هر ۳ ثانیه به‌روز می‌شود",
        ["هر کلید برای هر مدل جداگانه شمرده می‌شود: «در حال اجرا» با «حداکثر درخواست هم‌زمان» کلید، "
         "«شروع در یک دقیقه اخیر» با RPM و «درخواست امروز» با RPD مقایسه می‌شود.",
         "وقفه: پس از پاسخ ۴۲۹ ارائه‌دهنده، کلید برای مدت اعلام‌شده (یا ۶۰ ثانیه) استفاده نمی‌شود.",
         "وقتی ظرفیت کم است، نوبت بین کاربران می‌چرخد؛ هر سند منتظر، دلیل انتظار را در صفحه تحلیل نشان می‌دهد."],
        variant="blue", icon="info", expanded=False, key="engine_help",
    )
    engine_status()

with tab_defaults:
    st.subheader(":material/bolt: ظرفیت پردازش سرور", anchor=False)
    low, high = MAX_PARALLEL_CALLS_RANGE
    with st.form("server_cap_form", border=True):
        cap = st.number_input(
            "حداکثر درخواست هم‌زمان به ارائه‌دهندگان (کل سرور)", low, high, app_settings.max_parallel_calls(),
            help="سقف تعداد تحلیل‌هایی که هم‌زمان روی این سرور اجرا می‌شوند، برای همه کاربران و کلیدها با هم. "
                 "هر تحلیل در حال اجرا حافظه و اتصال شبکه مصرف می‌کند (برای یک PDF بیست‌مگابایتی تا حدود ۸۰ مگابایت).",
        )
        if st.form_submit_button("ذخیره", type="primary", icon=":material/save:"):
            try:
                app_settings.set_max_parallel_calls(int(cap))
                get_job_manager().scheduler.wake(refresh=True)
                st.toast("سقف هم‌زمانی سرور ذخیره شد و بلافاصله اعمال می‌شود.", icon=":material/check_circle:")
            except app_settings.SettingError as exc:
                show_errors(exc)
    st.caption("محدودیت هر کلید (هم‌زمانی، RPM و RPD) در زبانه «ارائه‌دهندگان، مدل‌ها و کلیدها» تنظیم می‌شود.")
    help_box(
        "سایر مقادیر پیش‌فرض",
        ["ویرایش مقادیر پیش‌فرض سهمیه‌ها، سقف بارگذاری و آمار مصرف در مرحله ۴ اضافه می‌شود."],
        variant="green", icon="construction",
    )
