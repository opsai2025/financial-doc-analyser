"""Provider / model / API-key editor shared by System settings (admin) and My profile (own providers)."""

from __future__ import annotations

import streamlit as st

from core.fa import fa_number, jalali_datetime
from core.models import ApiType, LlmModel
from services import providers as pv
from ui.components import confirm_button, help_box, md_escape, show_errors


def _limit(value: int | None) -> str:
    return "نامحدود" if value is None else fa_number(value)


def _optional_int(label: str, value: int | None, key: str, help_text: str | None = None, col=st) -> int | None:
    return col.number_input(label, min_value=1, value=value, step=1, key=key, help=help_text,
                            placeholder="خالی = نامحدود")


def _model_inputs(prefix: str, model: LlmModel | None) -> pv.ModelForm:
    c1, c2 = st.columns(2)
    with c1, st.container(key=f"fda_ltr_{prefix}_name"):
        name = st.text_input("شناسه مدل در API", value=model.name if model else "", key=f"{prefix}_name",
                             placeholder="مثال: gemini-2.5-pro")
    display = c2.text_input("نام نمایشی", value=model.display_name if model else "", key=f"{prefix}_display")
    c1, c2, c3 = st.columns(3)
    supports_pdf = c1.toggle("پشتیبانی از ورودی PDF", value=model.supports_pdf if model else True, key=f"{prefix}_pdf",
                             help="فقط مدل‌هایی که PDF را مستقیم می‌پذیرند برای تحلیل نمایش داده می‌شوند.")
    supports_temp = c2.toggle("ارسال دما (۰٫۵)", value=model.supports_temperature if model else True,
                              key=f"{prefix}_temp",
                              help="برای مدل‌هایی که پارامتر دما را نمی‌پذیرند خاموش کنید (تصمیم D-2.4).")
    active = c3.toggle("فعال", value=model.is_active if model else True, key=f"{prefix}_active")
    c1, c2 = st.columns(2)
    rpm = _optional_int("درخواست در دقیقه (RPM) — پیش‌فرض کلیدها", model.rpm if model else None, f"{prefix}_rpm",
                        col=c1)
    rpd = _optional_int("درخواست در روز (RPD) — پیش‌فرض کلیدها", model.rpd if model else None, f"{prefix}_rpd",
                        col=c2)
    c1, c2, c3, c4 = st.columns(4)
    max_file = _optional_int("حداکثر حجم PDF (مگابایت)", model.max_file_mb if model else None, f"{prefix}_maxfile",
                             "خالی = سقف عمومی سامانه (۲۰ مگابایت)", col=c1)
    attempts = c2.number_input("تعداد تلاش", 1, 10, model.max_attempts if model else 3, key=f"{prefix}_attempts")
    timeout = c3.number_input("مهلت پاسخ (ثانیه)", 10, 3600, model.timeout_s if model else 300, step=30,
                              key=f"{prefix}_timeout")
    max_out = _optional_int("سقف توکن خروجی", model.max_output_tokens if model else None, f"{prefix}_maxout",
                            "خالی = پیش‌فرض ارائه‌دهنده", col=c4)
    return pv.ModelForm(name=name, display_name=display, supports_pdf=supports_pdf, supports_temperature=supports_temp,
                        max_file_mb=max_file, rpm=rpm, rpd=rpd, max_attempts=attempts, timeout_s=timeout,
                        max_output_tokens=max_out, is_active=active)


def _open_next(prefix: str, *keys: str, close: tuple[str, ...] = ()) -> None:
    """Expander states to apply on the next run (a widget's state can't change after it was drawn)."""
    pending = st.session_state.setdefault(f"{prefix}_pending_open", {})
    pending.update({k: True for k in keys} | {k: False for k in close})


def _apply_pending(prefix: str) -> None:
    for key, value in st.session_state.pop(f"{prefix}_pending_open", {}).items():
        st.session_state[key] = value


def _models_section(owner_id: int | None, provider, prefix: str, root: str, models: list) -> None:
    """«افزودن مدل» first, then every model collapsed; content loads when opened (D-14.12)."""
    add = st.expander("افزودن مدل", icon=":material/add:", key=f"{prefix}_addmodel_exp", on_change="rerun")
    if add.open:
        with add:
            form = _model_inputs(f"{prefix}_newmodel", None)
            if st.button("افزودن مدل", type="primary", icon=":material/add:", key=f"{prefix}_newmodel_save"):
                try:
                    created = pv.save_model(owner_id, provider.id, form)
                    _open_next(root, f"{prefix}_m{created.id}_exp", close=(f"{prefix}_addmodel_exp",))
                    st.toast("مدل افزوده شد.", icon=":material/check_circle:")
                    st.rerun()
                except pv.ProviderError as exc:
                    show_errors(exc)
    if not models:
        st.caption("هنوز مدلی برای این ارائه‌دهنده تعریف نشده است.")
    for model in models:
        title = (f"**{md_escape(model.display_name)}** — `{model.name}` · {'فعال' if model.is_active else 'غیرفعال'}"
                 f" · RPM {_limit(model.rpm)} · RPD {_limit(model.rpd)}"
                 + ("" if model.supports_pdf else " · بدون پشتیبانی PDF"))
        item = st.expander(title, icon=":material/smart_toy:", key=f"{prefix}_m{model.id}_exp", on_change="rerun")
        if not item.open:
            continue
        with item:
            form = _model_inputs(f"{prefix}_m{model.id}", model)
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("ذخیره مدل", type="primary", icon=":material/save:", key=f"{prefix}_m{model.id}_save"):
                try:
                    pv.save_model(owner_id, provider.id, form, model.id)
                    st.toast("مدل ذخیره شد.", icon=":material/check_circle:")
                    st.rerun()
                except pv.ProviderError as exc:
                    show_errors(exc)
            with b2:
                if confirm_button("حذف مدل", key=f"{prefix}_m{model.id}_del",
                                  prompt="مدل حذف شود؟ تاریخچه تحلیل‌ها حفظ می‌شود."):
                    pv.delete_model(owner_id, provider.id, model.id)
                    st.rerun()


def _key_inputs(prefix: str, key, *, is_new: bool) -> pv.KeyForm:
    c1, c2 = st.columns(2)
    label = c1.text_input("برچسب کلید", value=key.label if key else "", key=f"{prefix}_label",
                          placeholder="مثال: کلید شماره ۱")
    with c2, st.container(key=f"fda_ltr_{prefix}_secret"):
        secret = st.text_input("مقدار کلید API" if is_new else "جایگزینی مقدار کلید (اختیاری)", type="password",
                               key=f"{prefix}_secret", help="کلید به‌صورت رمزگذاری‌شده ذخیره می‌شود و دیگر نمایش داده نمی‌شود.")
    c1, c2, c3, c4 = st.columns(4)
    rpm = _optional_int("RPM اختصاصی", key.rpm if key else None, f"{prefix}_rpm", "خالی = مقدار مدل", col=c1)
    rpd = _optional_int("RPD اختصاصی", key.rpd if key else None, f"{prefix}_rpd", "خالی = مقدار مدل", col=c2)
    concurrency = c3.number_input("حداکثر درخواست هم‌زمان", 1, 10, key.concurrency if key else 1, key=f"{prefix}_conc",
                                  help="چند تحلیل می‌تواند هم‌زمان با این کلید (برای هر مدل) اجرا شود. "
                                       "تغییر آن چند ثانیه بعد روی کارهای در حال اجرا هم اعمال می‌شود.")
    active = c4.toggle("فعال", value=key.is_active if key else True, key=f"{prefix}_active")
    return pv.KeyForm(label=label, secret=secret, rpm=rpm, rpd=rpd, concurrency=concurrency, is_active=active)


def _keys_section(owner_id: int | None, provider, prefix: str, root: str, keys: list) -> None:
    """«افزودن کلید» first, then every key collapsed; content loads when opened (D-14.12)."""
    add = st.expander("افزودن کلید API", icon=":material/add:", key=f"{prefix}_addkey_exp", on_change="rerun")
    if add.open:
        with add:
            form = _key_inputs(f"{prefix}_newkey", None, is_new=True)
            if st.button("افزودن کلید", type="primary", icon=":material/add:", key=f"{prefix}_newkey_save"):
                try:
                    created = pv.save_key(owner_id, provider.id, form)
                    for suffix in ("label", "secret"):
                        st.session_state.pop(f"{prefix}_newkey_{suffix}", None)
                    _open_next(root, f"{prefix}_k{created.id}_exp", close=(f"{prefix}_addkey_exp",))
                    st.toast("کلید افزوده شد.", icon=":material/check_circle:")
                    st.rerun()
                except pv.ProviderError as exc:
                    show_errors(exc)
    if not keys:
        st.caption("هنوز کلیدی برای این ارائه‌دهنده ثبت نشده است.")
    for key in keys:
        error = f" · آخرین خطا: {jalali_datetime(key.last_error_at)}" if key.last_error else ""
        title = (f"**{md_escape(key.label)}** — ••••{key.key_last4} · {'فعال' if key.is_active else 'غیرفعال'}"
                 f" · هم‌زمانی {fa_number(key.concurrency)}"
                 f" · RPM {_limit(key.rpm) if key.rpm else 'مطابق مدل'}"
                 f" · RPD {_limit(key.rpd) if key.rpd else 'مطابق مدل'}" + error)
        item = st.expander(title, icon=":material/key:", key=f"{prefix}_k{key.id}_exp", on_change="rerun")
        if not item.open:
            continue
        with item:
            if key.last_error:
                st.warning(f"آخرین خطا ({jalali_datetime(key.last_error_at)}): {md_escape(key.last_error)}",
                           icon=":material/error:")
            form = _key_inputs(f"{prefix}_k{key.id}", key, is_new=False)
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("ذخیره کلید", type="primary", icon=":material/save:", key=f"{prefix}_k{key.id}_save"):
                try:
                    pv.save_key(owner_id, provider.id, form, key.id)
                    st.toast("کلید ذخیره شد.", icon=":material/check_circle:")
                    st.rerun()
                except pv.ProviderError as exc:
                    show_errors(exc)
            with b2:
                if confirm_button("حذف کلید", key=f"{prefix}_k{key.id}_del", prompt="کلید برای همیشه حذف شود؟"):
                    pv.delete_key(owner_id, provider.id, key.id)
                    st.rerun()


def _test_section(provider, prefix: str, models: list, keys: list) -> None:
    if not models or not keys:
        st.info("برای آزمون اتصال، حداقل یک مدل و یک کلید تعریف کنید.", icon=":material/info:")
        return
    c1, c2 = st.columns(2)
    model = c1.selectbox("مدل", models, format_func=lambda m: m.display_name, key=f"{prefix}_test_model")
    key = c2.selectbox("کلید", keys, format_func=lambda k: f"{k.label} (••••{k.key_last4})", key=f"{prefix}_test_key")
    st.caption("یک درخواست متنی بسیار کوتاه ارسال می‌شود (چند توکن هزینه).")
    if st.button("آزمون اتصال", type="primary", icon=":material/network_check:", key=f"{prefix}_test_run"):
        with st.spinner("در حال ارسال درخواست آزمایشی…"):
            ok, message = pv.test_connection(model.id, key.id)
        (st.success if ok else st.error)(message, icon=":material/check_circle:" if ok else ":material/error:")


def _provider_inputs(prefix: str, provider) -> pv.ProviderForm:
    types = pv.available_api_types()
    c1, c2 = st.columns(2)
    name = c1.text_input("نام ارائه‌دهنده", value=provider.name if provider else "", key=f"{prefix}_pname",
                         placeholder="مثال: آوال‌ای (Gemini)")
    current = ApiType(provider.api_type) if provider and provider.api_type in types else types[0]
    api_type = c2.selectbox("نوع API", types, index=types.index(current), format_func=lambda t: pv.API_TYPE_LABELS[t],
                            key=f"{prefix}_ptype")
    c1, c2 = st.columns([3, 1])
    with c1, st.container(key=f"fda_ltr_{prefix}_purl"):
        base_url = st.text_input("آدرس پایه (Base URL)", value=(provider.base_url or "") if provider else "",
                                 key=f"{prefix}_purl", help=pv.BASE_URL_HINTS[api_type])
    active = c2.toggle("فعال", value=provider.is_active if provider else True, key=f"{prefix}_pactive")
    return pv.ProviderForm(name=name, api_type=api_type, base_url=base_url, is_active=active)


def provider_manager(owner_id: int | None, *, prefix: str) -> None:
    """Full editor (D-14.12). owner_id=None: admin-defined (shared) providers; otherwise the user's private ones.

    One list: «افزودن ارائه‌دهنده» first, then each provider collapsed with an informative title; inside
    it the same pattern for models and keys, then the connection test. Content loads when opened.
    """
    _apply_pending(prefix)
    providers = pv.list_providers(owner_id)
    add = st.expander("افزودن ارائه‌دهنده", icon=":material/add_business:", key=f"{prefix}_add_exp",
                      on_change="rerun")
    if add.open:
        with add:
            form = _provider_inputs(f"{prefix}_new", None)
            if st.button("افزودن ارائه‌دهنده", type="primary", icon=":material/add:", key=f"{prefix}_new_save"):
                try:
                    created = pv.save_provider(owner_id, form)
                    _open_next(prefix, f"{prefix}_p{created.id}_exp", close=(f"{prefix}_add_exp",))
                    st.toast("ارائه‌دهنده افزوده شد؛ اکنون مدل و کلید آن را تعریف کنید.", icon=":material/check_circle:")
                    st.rerun()
                except pv.ProviderError as exc:
                    show_errors(exc)
    if not providers:
        st.caption("هنوز ارائه‌دهنده‌ای تعریف نشده است.")
    for provider in providers:
        models, keys = pv.list_models(provider.id), pv.list_keys(provider.id)
        failing = sum(1 for k in keys if k.last_error)
        title = (f"**{md_escape(provider.name)}** — {pv.API_TYPE_LABELS.get(provider.api_type, provider.api_type)}"
                 f" · {'فعال' if provider.is_active else 'غیرفعال'} · {fa_number(len(models))} مدل"
                 f" · {fa_number(len(keys))} کلید" + (f" · {fa_number(failing)} کلید با خطا" if failing else ""))
        section = st.expander(title, icon=":material/hub:", key=f"{prefix}_p{provider.id}_exp", on_change="rerun")
        if not section.open:
            continue
        p_prefix = f"{prefix}_p{provider.id}"
        with section:
            form = _provider_inputs(p_prefix, provider)
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("ذخیره ارائه‌دهنده", type="primary", icon=":material/save:", key=f"{p_prefix}_save"):
                try:
                    pv.save_provider(owner_id, form, provider.id)
                    st.toast("ذخیره شد.", icon=":material/check_circle:")
                    st.rerun()
                except pv.ProviderError as exc:
                    show_errors(exc)
            with b2:
                if confirm_button("حذف ارائه‌دهنده", key=f"{p_prefix}_del",
                                  prompt="ارائه‌دهنده همراه با همه مدل‌ها، کلیدها و دسترسی‌هایش حذف شود؟"):
                    pv.delete_provider(owner_id, provider.id)
                    st.rerun()
            st.markdown("**:material/smart_toy: مدل‌ها**")
            _models_section(owner_id, provider, p_prefix, prefix, models)
            st.markdown("**:material/key: کلیدهای API**")
            _keys_section(owner_id, provider, p_prefix, prefix, keys)
            test = st.expander("آزمون اتصال", icon=":material/network_check:", key=f"{p_prefix}_test_exp",
                               on_change="rerun")
            if test.open:
                with test:
                    _test_section(provider, p_prefix, models, keys)


def provider_help(*, own: bool) -> None:
    items = [
        "ابتدا ارائه‌دهنده (نوع API و آدرس پایه) را تعریف کنید، سپس مدل‌ها و کلیدهای API آن را اضافه کنید.",
        "کلیدها رمزگذاری‌شده ذخیره می‌شوند و فقط چهار نویسه آخر آن‌ها نمایش داده می‌شود.",
        "محدودیت‌های RPM و RPD روی مدل (پیش‌فرض) تعریف می‌شوند و هر کلید می‌تواند مقدار اختصاصی داشته باشد؛ "
        "«حداکثر درخواست هم‌زمان» روی هر کلید تعیین می‌شود. این محدودیت‌ها برای هر کلید و مدل جداگانه شمرده می‌شوند.",
        "پیش از استفاده، با «آزمون اتصال» از درستی آدرس، کلید و نام مدل مطمئن شوید.",
    ]
    if own:
        items.append("ارائه‌دهندگان شما خصوصی هستند؛ تحلیل با کلیدهای شخصی از سهمیه ساعتی/روزانه کسر نمی‌شود "
                     "(برای مهمان، سقف کل اسناد همچنان اعمال می‌شود).")
    else:
        items.append("دسترسی کاربران به این ارائه‌دهندگان در زبانه «دسترسی‌ها» تعیین می‌شود.")
    help_box("راهنمای تعریف ارائه‌دهنده", items, variant="blue", icon="help", expanded=False,
             key=f"provider_help_{own}")
