import pandas as pd
import streamlit as st

from core.fa import fa_digits, fa_number, jalali_date, jalali_datetime
from core.models import Role, User, utcnow
from services.users import (
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    UserError,
    UserForm,
    create_user,
    default_form,
    form_from_user,
    list_users,
    reset_password,
    unlock_user,
    update_user,
    user_status,
)
from services.quota import MB, usage_overview
from ui.components import Card, cards_row, help_box, md_escape, page_title, show_errors
from ui.session import require_user

admin = require_user()
if not admin.is_admin:
    st.stop()

page_title("مدیریت کاربران", "manage_accounts", "ایجاد، ویرایش، تعیین نقش و سهمیه، بازنشانی رمز و غیرفعال‌سازی کاربران")

users = list_users()
usage = usage_overview()  # D-12.14
now = utcnow()

# ---- Status cards ------------------------------------------------------------------------
active_count = sum(1 for u in users if u.is_active and not u.is_expired(now))
problem_count = sum(1 for u in users if not u.is_active or u.is_expired(now) or u.is_locked(now))
cards_row(
    [
        Card("کل کاربران", fa_number(len(users)), "group", "info"),
        Card("کاربران فعال", fa_number(active_count), "verified_user", "success"),
        Card("تحلیلگران", fa_number(sum(1 for u in users if u.role == Role.ANALYST)), "query_stats", "neutral"),
        Card("مهمان‌ها", fa_number(sum(1 for u in users if u.role == Role.GUEST)), "person_outline", "neutral"),
        Card("غیرفعال یا قفل", fa_number(problem_count), "block", "danger" if problem_count else "neutral"),
    ],
    key="users",
)


def role_label(role: Role | str) -> str:
    return ROLE_LABELS[Role(role)]


def quota_summary(u: User) -> str:
    if u.role == Role.ANALYST:
        return fa_digits(
            f"ساعتی {u.docs_per_hour} · روزانه {u.docs_per_day} · نتایج {u.db_quota_mb} مگابایت · فایل {u.storage_quota_mb} مگابایت"
        )
    if u.role == Role.GUEST:
        return fa_digits(f"سقف {u.guest_doc_cap} سند · انقضا {jalali_date(u.expires_at)}")
    return "نامحدود" if u.role == Role.ADMIN else "—"


def quota_inputs(form: UserForm, prefix: str, *, is_new: bool, current_expiry=None) -> UserForm:
    """Role-specific quota fields (D-4.3/4.4/4.5). Returns the updated form."""
    if form.role is Role.ANALYST:
        c1, c2, c3, c4 = st.columns(4)
        form.docs_per_hour = c1.number_input("سقف اسناد در ساعت", 0, 10_000, form.docs_per_hour or 0, key=f"{prefix}_dph")
        form.docs_per_day = c2.number_input("سقف اسناد در روز", 0, 100_000, form.docs_per_day or 0, key=f"{prefix}_dpd")
        form.db_quota_mb = c3.number_input("سهمیه نتایج در پایگاه داده (مگابایت)", 0, 500, form.db_quota_mb or 0,
                                           key=f"{prefix}_dbq")
        form.storage_quota_mb = c4.number_input("سهمیه فایل‌های PDF (مگابایت)", 0, 1000, form.storage_quota_mb or 0,
                                                key=f"{prefix}_stq")
    elif form.role is Role.GUEST:
        c1, c2 = st.columns(2)
        form.guest_doc_cap = c1.number_input("سقف کل اسناد قابل تحلیل", 1, 1000, form.guest_doc_cap or 1,
                                             key=f"{prefix}_gcap")
        if is_new:
            form.guest_valid_days = c2.number_input("مدت اعتبار حساب (روز)", 1, 365, form.guest_valid_days or 14,
                                                    key=f"{prefix}_gdays")
        else:
            c2.markdown(f"**تاریخ انقضای فعلی:** {jalali_date(current_expiry)}")
            if c2.checkbox("تمدید / تغییر اعتبار", key=f"{prefix}_gext"):
                form.guest_valid_days = c2.number_input("اعتبار از امروز (روز)", 1, 365, 14, key=f"{prefix}_gdays")
            else:
                form.guest_valid_days = None
    elif form.role is Role.ADMIN:
        st.caption("مدیر سامانه محدودیت سهمیه ندارد.")
    else:
        st.caption("مشاهده‌گر امکان تحلیل ندارد و سهمیه برای او تعریف نمی‌شود.")
    return form


def show_secret_once(state_key: str) -> None:
    info = st.session_state.get(state_key)
    if not info:
        return
    with st.container(border=True):
        st.success(info["message"], icon=":material/key:")
        st.code(info["password"], language=None)
        st.caption("این رمز فقط یک بار نمایش داده می‌شود. آن را به‌صورت امن به کاربر تحویل دهید؛ "
                   "کاربر در اولین ورود باید آن را تغییر دهد.")
        if st.button("متوجه شدم", key=f"{state_key}_ack", icon=":material/done:"):
            del st.session_state[state_key]
            st.rerun()


tab_list, tab_create = st.tabs([":material/list: فهرست کاربران", ":material/person_add: ایجاد کاربر جدید"])

# ---- List & edit -------------------------------------------------------------------------
with tab_list:
    show_secret_once("users_reset_info")

    f1, f2 = st.columns([2, 3])
    search = f1.text_input("جستجو", placeholder="نام کاربری یا نام نمایشی", key="users_search")
    role_filter = f2.pills("نقش", list(Role), format_func=role_label, selection_mode="multi", key="users_role_filter")

    def matches(u: User) -> bool:
        if role_filter and Role(u.role) not in role_filter:
            return False
        term = search.strip().lower()
        return not term or term in u.username or term in u.display_name.lower()

    shown = [u for u in users if matches(u)]
    table = pd.DataFrame(
        [
            {
                "نام کاربری": u.username,
                "نام نمایشی": u.display_name,
                "نقش": role_label(u.role),
                "وضعیت": user_status(u, now),
                "سهمیه‌ها": quota_summary(u),
                "اسناد ۲۴ ساعت": fa_number(usage.get(u.id, (0, 0))[0]),
                "مصرف (مگابایت)": fa_digits(f"{usage.get(u.id, (0, 0))[1] / MB:.1f}"),
                "آخرین ورود": jalali_datetime(u.last_login_at) if u.last_login_at else "—",
                "تاریخ ایجاد": jalali_date(u.created_at),
            }
            for u in shown
        ]
    )
    # The grid renders left-to-right; reverse the columns so the first column sits on the right.
    event = st.dataframe(
        table[table.columns[::-1]],
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="users_table",
        column_config={"سهمیه‌ها": st.column_config.TextColumn(width="large")},
    )
    st.caption("برای ویرایش، روی یک ردیف کلیک کنید.")

    selected_rows = event.selection.rows if event else []
    if selected_rows and selected_rows[0] < len(shown):
        target = shown[selected_rows[0]]
        is_self = target.id == admin.id
        prefix = f"edit_{target.id}"
        with st.container(border=True):
            st.subheader(f":material/edit: ویرایش «{md_escape(target.display_name)}» ({target.username})", anchor=False)
            st.caption(f"وضعیت فعلی: {user_status(target, now)}")
            form = form_from_user(target)
            c1, c2, c3 = st.columns([2, 2, 1])
            form.display_name = c1.text_input("نام نمایشی", form.display_name, max_chars=128, key=f"{prefix}_name")
            new_role = c2.selectbox("نقش", list(Role), index=list(Role).index(form.role), format_func=role_label,
                                    disabled=is_self, key=f"{prefix}_role")
            if new_role is not form.role:
                # switching role: start from that role's defaults, keep name/active state
                defaults = default_form(new_role)
                defaults.display_name, defaults.is_active = form.display_name, form.is_active
                form = defaults
            form.is_active = c3.toggle("فعال", form.is_active, disabled=is_self, key=f"{prefix}_active")
            st.caption(ROLE_DESCRIPTIONS[form.role])
            form = quota_inputs(form, f"{prefix}_{form.role}", is_new=False, current_expiry=target.expires_at)
            if is_self:
                st.caption("نقش و وضعیت حساب خودتان قابل تغییر نیست.")

            b1, b2, b3, _ = st.columns([1, 1, 1, 2])
            if b1.button("ذخیره تغییرات", type="primary", icon=":material/save:", key=f"{prefix}_save", width="stretch"):
                try:
                    update_user(admin.id, target.id, form)
                    st.toast("تغییرات ذخیره شد.", icon=":material/check_circle:")
                    st.rerun()
                except UserError as exc:
                    show_errors(exc)
            if b2.button("بازنشانی رمز", icon=":material/lock_reset:", key=f"warn_{prefix}_reset", width="stretch"):
                temp = reset_password(admin.id, target.id)
                st.session_state.users_reset_info = {
                    "message": f"رمز موقت جدید برای «{target.username}» ایجاد شد.",
                    "password": temp,
                }
                st.rerun()
            if target.is_locked(now) and b3.button("رفع قفل", icon=":material/lock_open:", key=f"{prefix}_unlock",
                                                    width="stretch"):
                unlock_user(target.id)
                st.rerun()

# ---- Create ------------------------------------------------------------------------------
with tab_create:
    show_secret_once("users_created_info")
    if st.session_state.pop("create_reset", False):
        # Clear the form after a successful create (must happen before the widgets are drawn).
        st.session_state.update(create_username="", create_name="", create_password="", create_manual_pwd=False)
    help_box(
        "نقش‌ها",
        [f"**{ROLE_LABELS[r]}:** {ROLE_DESCRIPTIONS[r]}" for r in Role],
        variant="blue",
        icon="help",
        expanded=False,
        key="roles_help",
    )
    new_role = st.segmented_control("نقش کاربر جدید", list(Role), format_func=role_label, default=Role.ANALYST,
                                    key="create_role")
    new_role = new_role or Role.ANALYST
    form = default_form(new_role)
    c1, c2 = st.columns(2)
    with c1, st.container(key="fda_ltr_new_username"):
        username = st.text_input("نام کاربری (لاتین)", max_chars=32, key="create_username",
                                 help="۳ تا ۳۲ نویسه؛ حروف کوچک لاتین، رقم، نقطه، خط تیره یا زیرخط")
    form.display_name = c2.text_input("نام نمایشی", max_chars=128, key="create_name")
    form = quota_inputs(form, f"create_{new_role}", is_new=True)

    manual = st.toggle("تعیین دستی رمز موقت", key="create_manual_pwd",
                       help="در حالت پیش‌فرض، یک رمز موقت امن به‌صورت خودکار ساخته می‌شود.")
    manual_password = st.text_input("رمز موقت", type="password", key="create_password") if manual else None

    if st.button("ایجاد کاربر", type="primary", icon=":material/person_add:", key="create_submit"):
        try:
            user, temp = create_user(admin.id, username, form, manual_password or None)
            st.session_state.users_created_info = {
                "message": f"کاربر «{user.username}» با نقش {role_label(user.role)} ایجاد شد. رمز موقت:",
                "password": temp,
            }
            st.session_state.create_reset = True
            st.rerun()
        except UserError as exc:
            show_errors(exc)
