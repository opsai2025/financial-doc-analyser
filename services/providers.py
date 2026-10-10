"""Providers, models, API keys, access grants and connection tests (D-6, D-10.5, D-10.7)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from core.config import get_settings
from core.db import session_scope
from core.models import AccessGrant, ApiKey, ApiType, LlmModel, Provider, Role, User, utcnow
from core.security import decrypt_secret, encrypt_secret
from core.text_normalize import normalize_text
from services.llm import CallConfig, LlmError, adapter_for

API_TYPE_LABELS = {
    ApiType.GEMINI: "Google Gemini (API بومی)",
    ApiType.OPENAI_COMPATIBLE: "سازگار با OpenAI",
    ApiType.ANTHROPIC: "Anthropic Claude",
    ApiType.MOCK: "آزمایشی (Mock) — فقط محیط توسعه",
}
BASE_URL_HINTS = {
    ApiType.GEMINI: "اختیاری؛ برای درگاه‌هایی مانند https://api.avalai.ir — خالی = آدرس رسمی Google",
    ApiType.OPENAI_COMPATIBLE: "الزامی برای درگاه‌ها؛ مثال: https://api.avalai.ir/v1 — خالی = آدرس رسمی OpenAI",
    ApiType.ANTHROPIC: "اختیاری؛ خالی = آدرس رسمی Anthropic",
    ApiType.MOCK: "اختیاری؛ مثال: mock://?delay=1&fail_rate=0.2&fail_kind=rate_limit",
}


class ProviderError(ValueError):
    """Validation error with a Persian, user-facing message."""


def available_api_types() -> list[ApiType]:
    types = [ApiType.GEMINI, ApiType.OPENAI_COMPATIBLE, ApiType.ANTHROPIC]
    if not get_settings().is_production:
        types.append(ApiType.MOCK)
    return types


# --- Forms ------------------------------------------------------------------------------------


@dataclass
class ProviderForm:
    name: str
    api_type: str
    base_url: str = ""
    is_active: bool = True


@dataclass
class ModelForm:
    name: str
    display_name: str = ""
    supports_pdf: bool = True
    supports_temperature: bool = True
    max_file_mb: int | None = None
    rpm: int | None = None
    rpd: int | None = None
    max_attempts: int = 3
    timeout_s: int = 300
    max_output_tokens: int | None = None
    is_active: bool = True


@dataclass
class KeyForm:
    label: str
    secret: str = ""  # empty on edit = keep the stored key
    rpm: int | None = None
    rpd: int | None = None
    concurrency: int = 1
    is_active: bool = True


def _clean(value: str) -> str:
    return str(normalize_text(value or "")).strip()


def _validate_provider(form: ProviderForm) -> list[str]:
    errors = []
    if not 2 <= len(_clean(form.name)) <= 64:
        errors.append("نام ارائه‌دهنده باید بین ۲ تا ۶۴ نویسه باشد.")
    if form.api_type not in available_api_types():
        errors.append("نوع API معتبر نیست.")
    url = form.base_url.strip()
    if url and form.api_type != ApiType.MOCK and not re.match(r"^https?://[^\s]+$", url):
        errors.append("آدرس پایه باید با http:// یا https:// شروع شود.")
    return errors


def _validate_model(form: ModelForm) -> list[str]:
    errors = []
    if not 1 <= len(form.name.strip()) <= 128 or " " in form.name.strip():
        errors.append("شناسه مدل الزامی است و نباید فاصله داشته باشد (مانند gemini-2.5-pro).")
    for value, label in ((form.rpm, "RPM"), (form.rpd, "RPD"), (form.max_file_mb, "حداکثر حجم فایل"),
                         (form.max_output_tokens, "سقف توکن خروجی")):
        if value is not None and value < 1:
            errors.append(f"مقدار {label} باید خالی (نامحدود) یا بزرگ‌تر از صفر باشد.")
    if not 1 <= form.max_attempts <= 10:
        errors.append("تعداد تلاش باید بین ۱ تا ۱۰ باشد.")
    if not 10 <= form.timeout_s <= 3600:
        errors.append("مهلت پاسخ باید بین ۱۰ تا ۳۶۰۰ ثانیه باشد.")
    return errors


def _validate_key(form: KeyForm, *, is_new: bool) -> list[str]:
    errors = []
    if not 1 <= len(_clean(form.label)) <= 64:
        errors.append("برچسب کلید الزامی است (حداکثر ۶۴ نویسه).")
    if is_new and len(form.secret.strip()) < 8:
        errors.append("مقدار کلید API را وارد کنید.")
    for value, label in ((form.rpm, "RPM"), (form.rpd, "RPD")):
        if value is not None and value < 1:
            errors.append(f"مقدار {label} باید خالی (مقدار مدل) یا بزرگ‌تر از صفر باشد.")
    if not 1 <= form.concurrency <= 10:
        errors.append("هم‌زمانی هر کلید باید بین ۱ تا ۱۰ باشد.")
    return errors


def _raise(errors: list[str]) -> None:
    if errors:
        raise ProviderError("\n".join(errors))


# --- Ownership --------------------------------------------------------------------------------


def _owned_provider(db, provider_id: int, owner_id: int | None) -> Provider:
    provider = db.get(Provider, provider_id)
    if provider is None or provider.owner_id != owner_id:
        raise ProviderError("ارائه‌دهنده یافت نشد.")
    return provider


# --- Providers ----------------------------------------------------------------------------------


def list_providers(owner_id: int | None) -> list[Provider]:
    """Admin-defined providers (owner_id=None) or a user's private providers."""
    with session_scope() as db:
        query = select(Provider).order_by(Provider.name)
        query = query.where(Provider.owner_id.is_(None)) if owner_id is None else query.where(Provider.owner_id == owner_id)
        return list(db.scalars(query))


def save_provider(owner_id: int | None, form: ProviderForm, provider_id: int | None = None) -> Provider:
    _raise(_validate_provider(form))
    with session_scope() as db:
        duplicate = db.scalar(
            select(Provider.id).where(
                Provider.name == _clean(form.name),
                Provider.owner_id.is_(None) if owner_id is None else Provider.owner_id == owner_id,
                Provider.id != (provider_id or 0),
            )
        )
        if duplicate:
            raise ProviderError("ارائه‌دهنده‌ای با این نام وجود دارد.")
        provider = _owned_provider(db, provider_id, owner_id) if provider_id else Provider(owner_id=owner_id)
        provider.name = _clean(form.name)
        provider.api_type = form.api_type
        provider.base_url = form.base_url.strip() or None
        provider.is_active = form.is_active
        db.add(provider)
        db.flush()
        return provider


def delete_provider(owner_id: int | None, provider_id: int) -> None:
    with session_scope() as db:
        db.delete(_owned_provider(db, provider_id, owner_id))


# --- Models -------------------------------------------------------------------------------------


def list_models(provider_id: int) -> list[LlmModel]:
    with session_scope() as db:
        return list(db.scalars(select(LlmModel).where(LlmModel.provider_id == provider_id).order_by(LlmModel.display_name)))


def save_model(owner_id: int | None, provider_id: int, form: ModelForm, model_id: int | None = None) -> LlmModel:
    _raise(_validate_model(form))
    with session_scope() as db:
        _owned_provider(db, provider_id, owner_id)
        model = db.get(LlmModel, model_id) if model_id else LlmModel(provider_id=provider_id)
        if model is None or model.provider_id != provider_id:
            raise ProviderError("مدل یافت نشد.")
        for name in ("supports_pdf", "supports_temperature", "max_file_mb", "rpm", "rpd", "max_attempts",
                     "timeout_s", "max_output_tokens", "is_active"):
            setattr(model, name, getattr(form, name))
        model.name = form.name.strip()
        model.display_name = _clean(form.display_name) or model.name
        db.add(model)
        db.flush()
        return model


def delete_model(owner_id: int | None, provider_id: int, model_id: int) -> None:
    with session_scope() as db:
        _owned_provider(db, provider_id, owner_id)
        model = db.get(LlmModel, model_id)
        if model is not None and model.provider_id == provider_id:
            db.delete(model)


# --- API keys -------------------------------------------------------------------------------------


def list_keys(provider_id: int) -> list[ApiKey]:
    with session_scope() as db:
        return list(db.scalars(select(ApiKey).where(ApiKey.provider_id == provider_id).order_by(ApiKey.label)))


def save_key(owner_id: int | None, provider_id: int, form: KeyForm, key_id: int | None = None) -> ApiKey:
    _raise(_validate_key(form, is_new=key_id is None))
    with session_scope() as db:
        _owned_provider(db, provider_id, owner_id)
        key = db.get(ApiKey, key_id) if key_id else ApiKey(provider_id=provider_id)
        if key is None or key.provider_id != provider_id:
            raise ProviderError("کلید یافت نشد.")
        secret = form.secret.strip()
        if secret:
            key.key_encrypted = encrypt_secret(secret)
            key.key_last4 = secret[-4:]
            key.last_error = key.last_error_at = None
        key.label = _clean(form.label)
        key.rpm, key.rpd = form.rpm, form.rpd
        key.concurrency = form.concurrency
        key.is_active = form.is_active
        db.add(key)
        db.flush()
        return key


def delete_key(owner_id: int | None, provider_id: int, key_id: int) -> None:
    with session_scope() as db:
        _owned_provider(db, provider_id, owner_id)
        key = db.get(ApiKey, key_id)
        if key is not None and key.provider_id == provider_id:
            db.delete(key)


def record_key_error(key_id: int, message: str | None) -> None:
    with session_scope() as db:
        key = db.get(ApiKey, key_id)
        if key is not None:
            key.last_error = message or None
            key.last_error_at = utcnow() if message else None


# --- Access grants (D-6.3) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Subject:
    role: Role | None = None
    user_id: int | None = None


def get_grant(subject: Subject, provider_id: int, effect: str = "allow") -> AccessGrant | None:
    with session_scope() as db:
        return db.scalar(_grant_query(subject, provider_id, effect))


def _grant_query(subject: Subject, provider_id: int, effect: str):
    query = select(AccessGrant).where(AccessGrant.provider_id == provider_id, AccessGrant.effect == effect)
    if subject.role is not None:
        return query.where(AccessGrant.role == subject.role, AccessGrant.user_id.is_(None))
    return query.where(AccessGrant.user_id == subject.user_id, AccessGrant.role.is_(None))


def set_grant(subject: Subject, provider_id: int, effect: str, model_ids: list[int] | None,
              api_key_ids: list[int] | None) -> None:
    """Create/replace the subject's grant for a provider. Lists: None = all, [] is rejected."""
    if effect == "deny" and subject.role is not None:
        raise ProviderError("محرومیت فقط برای کاربر قابل تعریف است.")
    if model_ids == [] or api_key_ids == []:
        raise ProviderError("حداقل یک مدل و یک کلید انتخاب کنید یا گزینه «همه» را بزنید.")
    with session_scope() as db:
        provider = db.get(Provider, provider_id)
        if provider is None or provider.owner_id is not None:
            raise ProviderError("دسترسی فقط برای ارائه‌دهندگان تعریف‌شده توسط مدیر قابل تنظیم است.")
        grant = db.scalar(_grant_query(subject, provider_id, effect)) or AccessGrant(
            role=subject.role, user_id=subject.user_id, provider_id=provider_id, effect=effect
        )
        grant.model_ids = sorted(model_ids) if model_ids is not None else None
        grant.api_key_ids = sorted(api_key_ids) if api_key_ids is not None else None
        db.add(grant)


def clear_grant(subject: Subject, provider_id: int, effect: str = "allow") -> None:
    with session_scope() as db:
        grant = db.scalar(_grant_query(subject, provider_id, effect))
        if grant is not None:
            db.delete(grant)


def _matches(grant: AccessGrant, model_id: int, key_id: int) -> bool:
    return (grant.model_ids is None or model_id in grant.model_ids) and (
        grant.api_key_ids is None or key_id in grant.api_key_ids
    )


@dataclass(frozen=True)
class KeyChoice:
    id: int
    label: str
    last4: str


@dataclass(frozen=True)
class ModelChoice:
    provider_id: int
    provider_name: str
    api_type: str
    model_id: int
    model_name: str
    display_name: str
    own: bool  # the user's private provider (doc quotas waived, D-6.4)
    max_file_mb: int | None
    keys: tuple[KeyChoice, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        suffix = " (کلید شخصی)" if self.own else ""
        return f"{self.provider_name} › {self.display_name}{suffix}"


def usable_models(user_id: int) -> list[ModelChoice]:
    """Active, PDF-capable models the user may analyse with, each with its usable keys."""
    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None or user.role == Role.VIEWER:
            return []
        providers = list(db.scalars(
            select(Provider).where(Provider.is_active.is_(True))
            .where((Provider.owner_id.is_(None)) | (Provider.owner_id == user_id))
        ))
        grants = list(db.scalars(
            select(AccessGrant).where((AccessGrant.role == user.role) | (AccessGrant.user_id == user_id))
        ))
        choices = []
        for provider in sorted(providers, key=lambda p: (p.owner_id is not None, p.name)):
            own = provider.owner_id == user_id
            models = db.scalars(select(LlmModel).where(
                LlmModel.provider_id == provider.id, LlmModel.is_active.is_(True), LlmModel.supports_pdf.is_(True)))
            keys = list(db.scalars(select(ApiKey).where(ApiKey.provider_id == provider.id, ApiKey.is_active.is_(True))))
            allows = [g for g in grants if g.provider_id == provider.id and g.effect == "allow"]
            denies = [g for g in grants if g.provider_id == provider.id and g.effect == "deny"]
            for model in models:
                usable = [
                    KeyChoice(k.id, k.label, k.key_last4)
                    for k in keys
                    if own or user.role == Role.ADMIN
                    or (any(_matches(g, model.id, k.id) for g in allows) and not any(_matches(g, model.id, k.id) for g in denies))
                ]
                if usable:
                    choices.append(ModelChoice(provider.id, provider.name, provider.api_type, model.id, model.name,
                                               model.display_name, own, model.max_file_mb, tuple(usable)))
        return choices


# --- Runtime config --------------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyLimits:
    """Effective capacity of one key for one model (D-11.9): key value, else the model default."""

    rpm: int | None  # None = unlimited
    rpd: int | None
    concurrency: int
    active: bool = True


def _limits(model: LlmModel, key: ApiKey) -> KeyLimits:
    return KeyLimits(rpm=key.rpm or model.rpm, rpd=key.rpd or model.rpd, concurrency=max(1, key.concurrency),
                     active=bool(key.is_active and model.is_active))


@dataclass(frozen=True)
class KeyRuntime:
    key_id: int
    label: str
    cfg: CallConfig
    limits: KeyLimits


def runtime_keys(model_id: int, key_ids: list[int]) -> tuple[LlmModel, Provider, list[KeyRuntime]]:
    """Decrypted call configs for the selected keys of a model (inactive keys are dropped)."""
    with session_scope() as db:
        model = db.get(LlmModel, model_id)
        if model is None:
            raise ProviderError("مدل یافت نشد.")
        provider = db.get(Provider, model.provider_id)
        runtimes = []
        for key in db.scalars(select(ApiKey).where(ApiKey.id.in_(key_ids), ApiKey.provider_id == provider.id)
                              .order_by(ApiKey.id)):
            if not key.is_active:
                continue
            cfg = CallConfig(
                api_type=provider.api_type, model=model.name, api_key=decrypt_secret(key.key_encrypted),
                base_url=provider.base_url, timeout_s=model.timeout_s, send_temperature=model.supports_temperature,
                max_output_tokens=model.max_output_tokens,
            )
            runtimes.append(KeyRuntime(key.id, key.label, cfg, _limits(model, key)))
        return model, provider, runtimes


def live_limits(pairs: set[tuple[int, int]]) -> dict[tuple[int, int], KeyLimits]:
    """Current limits for (key_id, model_id) pairs, so edits apply to running work (D-11.9).

    A deleted key or model is reported as inactive.
    """
    if not pairs:
        return {}
    with session_scope() as db:
        keys = {k.id: k for k in db.scalars(select(ApiKey).where(ApiKey.id.in_({k for k, _ in pairs})))}
        models = {m.id: m for m in db.scalars(select(LlmModel).where(LlmModel.id.in_({m for _, m in pairs})))}
        return {
            (key_id, model_id): (_limits(models[model_id], keys[key_id]) if key_id in keys and model_id in models
                                 else KeyLimits(None, None, 1, active=False))
            for key_id, model_id in pairs
        }


def test_connection(model_id: int, key_id: int) -> tuple[bool, str]:
    """Tiny text-only request with the given key and model (D-10.7)."""
    try:
        model, provider, keys = runtime_keys(model_id, [key_id])
        if not keys:
            return False, "کلید فعال یافت نشد."
        reply = adapter_for(provider.api_type).test_connection(keys[0].cfg)
    except LlmError as exc:
        record_key_error(key_id, str(exc))
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced to the admin as-is
        return False, f"{type(exc).__name__}: {exc}"
    record_key_error(key_id, None)
    return True, f"اتصال برقرار شد. پاسخ مدل: «{reply[:60]}»"


def purge_expired_guest_providers() -> int:
    """Delete private providers of expired guests (D-6.4)."""
    with session_scope() as db:
        now = utcnow()
        guests = [u.id for u in db.scalars(select(User).where(User.role == Role.GUEST)) if u.is_expired(now)]
        if not guests:
            return 0
        providers = list(db.scalars(select(Provider).where(Provider.owner_id.in_(guests))))
        for provider in providers:
            db.delete(provider)
        return len(providers)
