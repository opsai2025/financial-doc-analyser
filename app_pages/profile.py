from itertools import groupby

import pandas as pd
import streamlit as st

from core.fa import fa_digits, fa_number, jalali_date, jalali_datetime
from core.models import Role, as_utc, utcnow
from services import app_settings
from services.providers import API_TYPE_LABELS, usable_models
from services.quota import MB, quota_status
from services.users import ROLE_DESCRIPTIONS, ROLE_LABELS
from ui.components import Card, cards_row, help_box, md_escape, page_title, usage_bar
from ui.forms import password_change_form
from ui.provider_editor import provider_help, provider_manager
from ui.session import require_user

user = require_user()

page_title("پروفایل من", "account_circle", "اطلاعات حساب، سهمیه‌ها و تغییر رمز عبور")


def mb(value: int) -> str:
    return fa_digits(f"{value / MB:.1f}")


can_own_providers = user.role in (Role.ANALYST, Role.GUEST)  # D-10.12
tab_labels = [":material/badge: اطلاعات حساب", ":material/password: تغییر رمز عبور"]
if can_own_providers:  # shared providers first, then the user's own (D-11.6)
    tab_labels += [":material/share: ارائه‌دهندگان سامانه", ":material/hub: ارائه‌دهندگان من"]
tabs = st.tabs(tab_labels)
tab_info, tab_password = tabs[0], tabs[1]

with tab_info:
    cards = [
        Card("نقش", ROLE_LABELS[user.role], "badge", "info"),
        Card("آخرین ورود", jalali_datetime(user.last_login_at), "schedule", "neutral"),
    ]
    if user.role is Role.GUEST:
        cards.append(Card("سقف کل اسناد", fa_number(user.guest_doc_cap or 0), "description", "warning"))
        cards.append(Card("تاریخ انقضای حساب", jalali_date(user.expires_at), "event", "warning"))
    cards_row(cards, key="profile")

    st.markdown(f"**نام نمایشی:** {md_escape(user.display_name)}  \n**نام کاربری:** `{user.username}`")
    st.caption(ROLE_DESCRIPTIONS[user.role])

    quota = quota_status(user.id)
    if user.role is Role.ANALYST:
        st.subheader(":material/speed: مصرف سهمیه", anchor=False)
        hour, day = quota.hour_limit or 0, quota.day_limit or 0
        usage_bar("اسناد در ۶۰ دقیقه اخیر", f"{fa_number(quota.hour_used)} از {fa_number(hour)}",
                  quota.hour_used / hour if hour else 1)
        usage_bar("اسناد در ۲۴ ساعت اخیر", f"{fa_number(quota.day_used)} از {fa_number(day)}",
                  quota.day_used / day if day else 1)
        db_quota, st_quota = quota.db_quota_bytes or 0, quota.storage_quota_bytes or 0
        usage_bar("فضای نتایج", f"{mb(quota.db_used_bytes)} از {mb(db_quota)} مگابایت",
                  quota.db_used_bytes / db_quota if db_quota else 1)
        usage_bar("فضای فایل‌ها", f"{mb(quota.storage_used_bytes)} از {mb(st_quota)} مگابایت",
                  quota.storage_used_bytes / st_quota if st_quota else 1)
        st.caption("تحلیل با کلیدهای شخصی (ارائه‌دهندگان من) و استفاده از نتایج قبلی از سهمیه اسناد کم نمی‌کند. "
                   "سهمیه‌ها را مدیر سامانه تعیین می‌کند.")
    elif user.role is Role.GUEST:
        st.subheader(":material/speed: مصرف سهمیه", anchor=False)
        cap = quota.guest_cap or 0
        usage_bar("اسناد تحلیل‌شده از سقف کل", f"{fa_number(quota.guest_used)} از {fa_number(cap)}",
                  quota.guest_used / cap if cap else 1)
        if user.expires_at:
            days_left = max(0, (as_utc(user.expires_at) - utcnow()).days)
            st.caption(f":material/event: {fa_number(days_left)} روز تا پایان اعتبار حساب "
                       f"({jalali_date(user.expires_at)}).")
    elif user.role is Role.ADMIN:
        st.caption("مدیر سامانه محدودیت سهمیه ندارد.")

with tab_password:
    password_change_form(user, form_key="profile_change")
    st.caption("پس از تغییر رمز، تمام نشست‌های دیگر شما (مرورگرها و دستگاه‌های دیگر) خارج می‌شوند.")




def shared_providers_view() -> None:
    """Providers, models and keys the admin made available to this user (read-only, D-11.6).

    Uses the same authorization as the analysis page (`usable_models`): role defaults plus the
    user's own allow/deny overrides, active providers/models/keys only.
    """
    shared = [c for c in usable_models(user.id) if not c.own]
    help_box(
        "ارائه‌دهندگانی که مدیر سامانه در اختیار شما گذاشته است",
        ["این فهرست فقط مدل‌ها و کلیدهایی را نشان می‌دهد که شما مجاز به استفاده از آن‌ها هستید.",
         "این موارد توسط مدیر سامانه تعریف و نگه‌داری می‌شوند و قابل ویرایش نیستند؛ کلیدها فقط با چهار نویسه آخر نمایش داده می‌شوند.",
         "تحلیل با این کلیدها از سهمیه تحلیل شما کسر می‌شود. ارائه‌دهندگان شخصی شما در زبانه «ارائه‌دهندگان من» هستند."],
        variant="blue", icon="info",
    )
    if not shared:
        st.info("مدیر سامانه هنوز ارائه‌دهنده‌ای در اختیار شما قرار نداده است.", icon=":material/info:")
        return
    cards_row([
        Card("ارائه‌دهنده", fa_number(len({c.provider_id for c in shared})), "share", "info"),
        Card("مدل", fa_number(len(shared)), "model_training", "info"),
        Card("کلید API", fa_number(len({k.id for c in shared for k in c.keys})), "key", "info"),
    ], key="shared_prov")
    max_pdf_mb = app_settings.get("max_pdf_mb")
    for _, group in groupby(shared, key=lambda c: c.provider_id):
        models = list(group)
        first = models[0]
        with st.container(border=True):
            st.markdown(f"**:material/hub: {md_escape(first.provider_name)}** — "
                        f"{API_TYPE_LABELS.get(first.api_type, first.api_type)}")
            table = pd.DataFrame([{
                "مدل": c.display_name,
                "شناسه مدل": c.model_name,
                "سقف حجم هر فایل (مگابایت)": fa_number(min(max_pdf_mb, c.max_file_mb or max_pdf_mb)),
                "کلیدهای قابل استفاده": "، ".join(f"{k.label} (••••{k.last4})" for k in c.keys),
            } for c in models])
            st.dataframe(table[table.columns[::-1]], hide_index=True, column_config={
                "کلیدهای قابل استفاده": st.column_config.TextColumn(width="large")})


if can_own_providers:
    with tabs[2]:
        shared_providers_view()
    with tabs[3]:
        provider_help(own=True)
        provider_manager(user.id, prefix="own_prov")
