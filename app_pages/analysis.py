import pandas as pd
import streamlit as st

from core.config import get_settings
from core.fa import fa_digits, fa_number, jalali_date, jalali_datetime
from core.models import Role
from services import app_settings
from services import providers as pv
from services.jobs import JobError, Version, default_version, get_job_manager, order_versions
from services.quota import MB, quota_status
from services.uploads import from_pdfs, from_zip
from ui.components import Card, cards_row, file_label, help_box, md_escape, page_title, show_errors
from ui.request_view import show_request
from ui.session import require_user

user = require_user()
if user.role is Role.VIEWER:
    st.stop()
manager = get_job_manager()

page_title("تحلیل اسناد", "upload_file", "بارگذاری گزارش‌های حسابرسی، انتخاب مدل و پیگیری وضعیت پردازش")

NEW = ""  # version picker: "new analysis"


def mb(value: int) -> str:
    return fa_digits(f"{value / MB:.1f}")


def version_label(v: Version) -> str:
    label = f"{jalali_datetime(v.created_at)} — {v.provider_name} › {v.model_name}"
    if v.has_warnings:
        label += " — با هشدار"
    if v.user_id != user.id:  # admins see everyone's results (D-5.6)
        label += f" — کاربر {v.username}"
    return label


# ---- Status cards: what the user can still do (rules: indicators first) ----------------------
quota = quota_status(user.id)
if user.role is Role.GUEST:
    left = quota.remaining_docs(own_keys=False, reserved=manager.reserved(user.id, False))
    cards_row([
        Card("اسناد باقی‌مانده", fa_number(left), "description", "danger" if left == 0 else "success"),
        Card("سقف کل اسناد", fa_number(quota.guest_cap or 0), "inventory_2", "neutral"),
        Card("اعتبار حساب تا", jalali_date(user.expires_at), "event", "warning"),
    ], key="an_quota")
elif user.role is Role.ANALYST:
    hour_left = max(0, (quota.hour_limit or 0) - quota.hour_used)
    day_left = max(0, (quota.day_limit or 0) - quota.day_used)
    db_room, st_room = quota.db_room() or 0, quota.storage_room() or 0
    cards_row([
        Card("باقی‌مانده در این ساعت", fa_number(hour_left), "hourglass_top", "danger" if hour_left == 0 else "success"),
        Card("باقی‌مانده در ۲۴ ساعت", fa_number(day_left), "calendar_today", "danger" if day_left == 0 else "success"),
        Card("فضای نتایج", f"{mb(quota.db_used_bytes)} از {mb(quota.db_quota_bytes or 0)} مگابایت", "database",
             "danger" if db_room == 0 else "info"),
        Card("فضای فایل‌ها", f"{mb(quota.storage_used_bytes)} از {mb(quota.storage_quota_bytes or 0)} مگابایت", "cloud",
             "warning" if st_room == 0 else "info"),
    ], key="an_quota")
else:
    st.caption(":material/all_inclusive: مدیر سامانه محدودیت سهمیه ندارد.")


def pick_versions(files, versions: dict[str, list[Version]], choice, nonce: int) -> dict[str, str]:
    """One dropdown per file with earlier results (D-12.1). Returns document_id -> request_id to reuse."""
    first = {}
    for f in files:
        first.setdefault(f.sha256, f)
    shas = [sha for sha in first if versions.get(sha)]
    if not shas:
        return {}
    keys = {sha: f"an_ver_{nonce}_{choice.model_id}_{sha[:16]}" for sha in shas}
    defaults = {sha: default_version(versions[sha], choice.provider_name, choice.model_name) or NEW for sha in shas}
    for sha in shas:
        st.session_state.setdefault(keys[sha], defaults[sha])

    def set_all(latest: bool) -> None:
        for sha in shas:
            st.session_state[keys[sha]] = defaults[sha] if latest else NEW

    st.markdown(f"**:material/history: اسنادی که نتیجه قبلی دارند ({fa_number(len(shas))})**")
    st.caption("برای هر سند یکی را انتخاب کنید: تحلیل جدید یا استفاده از یکی از نتایج قبلی (بدون هزینه و سهمیه). "
               "نتایج همین مدل اول آمده‌اند؛ نتیجه مدل‌های دیگر هم قابل انتخاب است.")
    b1, b2, _ = st.columns([1, 1, 2])
    b1.button("همه: آخرین نتیجه همین مدل", icon=":material/history:", key=f"an_ver_latest_{nonce}",
              on_click=set_all, args=(True,), width="stretch")
    b2.button("همه: تحلیل جدید", icon=":material/refresh:", key=f"an_ver_new_{nonce}",
              on_click=set_all, args=(False,), width="stretch")
    reuse = {}
    for sha in shas:
        ordered = order_versions(versions[sha], choice.provider_name, choice.model_name)
        labels = {NEW: "تحلیل جدید", **{v.request_id: version_label(v) for v in ordered}}
        c1, c2 = st.columns([2, 5], vertical_alignment="center")
        c1.markdown(md_escape(file_label(first[sha].name)))
        picked = c2.selectbox("نسخه", list(labels), format_func=labels.get, key=keys[sha],
                              label_visibility="collapsed")
        if picked != NEW:
            reuse[sha] = picked
    return reuse


# ---- New request ---------------------------------------------------------------------------
choices = pv.usable_models(user.id)
with st.container(border=True):
    st.subheader(":material/add_task: درخواست جدید", anchor=False)
    if not choices:
        help_box(
            "هیچ مدلی برای تحلیل در دسترس شما نیست",
            ["مدیر سامانه باید در «تنظیمات سامانه» ارائه‌دهنده، مدل و کلید تعریف و به نقش یا حساب شما دسترسی بدهد."]
            if user.is_admin else
            ["از مدیر سامانه بخواهید به حساب شما دسترسی بدهد،",
             "یا در «پروفایل من › ارائه‌دهندگان من» ارائه‌دهنده و کلید شخصی خود را تعریف کنید."],
            variant="green", icon="info",
        )
    else:
        c1, c2 = st.columns([3, 2])
        choice = c1.selectbox("مدل", choices, format_func=lambda c: c.label, key="an_model")
        key_ids = c2.multiselect(
            "کلیدهای API", [k.id for k in choice.keys], default=[k.id for k in choice.keys],
            format_func={k.id: f"{k.label} (••••{k.last4})" for k in choice.keys}.get, key=f"an_keys_{choice.model_id}",
        )
        mode = st.segmented_control("روش بارگذاری", ["pdf", "zip"], default="pdf", key="an_mode",
                                    format_func={"pdf": ":material/picture_as_pdf: فایل‌های PDF",
                                                 "zip": ":material/folder_zip: فایل ZIP"}.get) or "pdf"
        nonce = st.session_state.setdefault("an_nonce", 0)
        limits = app_settings.upload_limits()
        if mode == "pdf":
            uploads = st.file_uploader("فایل‌های PDF گزارش حسابرسی", type=["pdf"], accept_multiple_files=True,
                                       key=f"an_pdfs_{nonce}", max_upload_size=limits.max_pdf_mb)
            st.caption(f"حداکثر {fa_digits(limits.max_pdf_mb)} مگابایت برای هر فایل PDF و "
                       f"{fa_digits(limits.max_files)} فایل در هر درخواست.")
            intake = from_pdfs([(f.name, f.getvalue()) for f in uploads], limits) if uploads else None
        else:
            upload = st.file_uploader("فایل ZIP شامل گزارش‌ها (زیرپوشه‌ها مجاز است)", type=["zip"], key=f"an_zip_{nonce}",
                                      max_upload_size=limits.max_zip_mb)
            st.caption(f"حداکثر {fa_digits(limits.max_zip_mb)} مگابایت؛ فایل‌های غیر PDF نادیده گرفته می‌شوند.")
            intake = from_zip(upload.getvalue(), limits) if upload else None

        if intake is not None:
            if intake.rejected:
                with st.expander(f":material/warning: {fa_number(len(intake.rejected))} مورد نادیده گرفته شد", expanded=True):
                    st.markdown("\n".join(f"- {md_escape(r)}" for r in intake.rejected))
            if intake.ok:
                versions = manager.previous_versions(user, [f.sha256 for f in intake.files])
                plan_box = st.container()  # the overview is drawn above the per-file choices
                reuse = pick_versions(intake.files, versions, choice, nonce)

                seen, plan_rows, new_count = set(), [], 0
                for f in intake.files:
                    sha = f.sha256
                    if sha in seen:
                        plan = "تکراری – نادیده گرفته می‌شود"
                    elif sha in reuse:
                        version = next(v for v in versions[sha] if v.request_id == reuse[sha])
                        plan = f"استفاده از نتیجه {jalali_datetime(version.created_at)}"
                        if (version.provider_name, version.model_name) != (choice.provider_name, choice.model_name):
                            plan += f" ({version.provider_name} › {version.model_name})"
                    else:
                        plan, new_count = "تحلیل جدید", new_count + 1
                    seen.add(sha)
                    plan_rows.append({"فایل": f.name, "حجم (مگابایت)": mb(f.size), "برنامه": plan})
                with plan_box:
                    table = pd.DataFrame(plan_rows)
                    st.dataframe(table[table.columns[::-1]], hide_index=True)

                remaining = quota.remaining_docs(choice.own, reserved=manager.reserved(user.id, choice.own))
                if remaining is not None and new_count > remaining:
                    st.warning(f"از {fa_number(new_count)} سند جدید فقط {fa_number(remaining)} سند در سهمیه فعلی شما "
                               "پردازش می‌شود؛ بقیه «رد شده – سهمیه» ثبت می‌شوند و بعداً قابل تلاش مجدد هستند.",
                               icon=":material/warning:")
                if user.role is Role.GUEST:
                    st.info("حساب مهمان: نتایج ذخیره نمی‌شوند و تا "
                            f"{fa_number(get_settings().guest_result_ttl_minutes)} دقیقه پس از پایان در همین صفحه "
                            "قابل مشاهده و دریافت اکسل هستند.", icon=":material/info:")
                elif quota.db_room() == 0:
                    st.warning("سهمیه ذخیره نتایج شما پر است؛ نتایج جدید ذخیره نمی‌شوند و فقط به‌صورت موقت قابل "
                               "مشاهده و خروجی اکسل هستند.", icon=":material/database:")
                if user.role is Role.ANALYST and quota.storage_room() == 0:
                    st.caption(":material/cloud_off: فضای ذخیره فایل شما پر است؛ فایل‌های PDF نگه‌داری نمی‌شوند.")

                if st.button("شروع تحلیل", type="primary", icon=":material/rocket_launch:", key="an_start",
                             disabled=not key_ids):
                    try:
                        request_id = manager.submit(user, choice, key_ids, intake.files, reuse=reuse)
                        st.session_state.an_current = request_id
                        st.session_state.an_nonce = nonce + 1
                        st.rerun()
                    except JobError as exc:
                        show_errors(exc)


# ---- Current request -------------------------------------------------------------------------
current = st.session_state.get("an_current")
if current is None and user.role is Role.GUEST:  # the guest's last request (D-4.11)
    latest = manager.latest_ephemeral(user.id)
    current = latest.id if latest else None
if current:
    st.subheader(":material/monitoring: وضعیت درخواست", anchor=False)
    show_request(current, user, key="an", on_deleted=lambda: st.session_state.pop("an_current", None))
if user.role is not Role.GUEST:
    st.page_link("app_pages/history.py", label="همه درخواست‌ها، جستجو، اشتراک‌گذاری و حذف در «تاریخچه تحلیل‌ها»",
                 icon=":material/history:")
