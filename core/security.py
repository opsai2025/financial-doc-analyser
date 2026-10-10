"""Password hashing, password policy and signed session tokens."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def _prehash(password: str) -> bytes:
    # bcrypt only uses the first 72 bytes (and bcrypt>=5 rejects longer input); Persian
    # characters are 2 bytes in UTF-8, so pre-hash to a fixed-length value first.
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except ValueError:
        return False


# Used to keep login timing similar when the username does not exist.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_policy_errors(password: str) -> list[str]:
    """Return Persian messages for every violated rule (empty list = valid)."""
    errors = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"رمز عبور باید حداقل {PASSWORD_MIN_LENGTH} نویسه باشد.")
    if len(password) > PASSWORD_MAX_LENGTH:
        errors.append(f"رمز عبور نباید بیش از {PASSWORD_MAX_LENGTH} نویسه باشد.")
    if not re.search(r"[^\W\d_]", password):
        errors.append("رمز عبور باید حداقل یک حرف داشته باشد.")
    if not re.search(r"\d", password):
        errors.append("رمز عبور باید حداقل یک رقم داشته باشد.")
    if password != password.strip():
        errors.append("رمز عبور نباید با فاصله شروع یا تمام شود.")
    return errors


def generate_temp_password(length: int = 12) -> str:
    """Readable temporary password that satisfies the policy (no ambiguous characters)."""
    letters = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"
    digits = "23456789"
    alphabet = letters + digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c in letters for c in pwd) and any(c in digits for c in pwd):
            return pwd


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="fda-session-v1")


def sign_session(secret: str, user_id: int, session_version: int) -> str:
    return _serializer(secret).dumps({"uid": user_id, "sv": session_version})


def read_session(secret: str, token: str, max_age_seconds: int) -> tuple[int, int] | None:
    """Return (user_id, session_version) for a valid, unexpired token."""
    try:
        data = _serializer(secret).loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("uid"), int) or not isinstance(data.get("sv"), int):
        return None
    return data["uid"], data["sv"]


USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{2,31}$")
USERNAME_HINT = "نام کاربری: ۳ تا ۳۲ نویسه، با حرف لاتین شروع شود و فقط شامل حروف کوچک لاتین، رقم، نقطه، خط تیره یا زیرخط باشد."



# --- API key encryption at rest (D-6.6) ---------------------------------------------------


def _fernet():
    from cryptography.fernet import Fernet

    from core.config import ConfigError, get_settings

    key = get_settings().app_encryption_key
    if not key:
        raise ConfigError("APP_ENCRYPTION_KEY is not set; API keys cannot be stored (see .env.example).")
    try:
        return Fernet(key.encode("ascii"))
    except ValueError as exc:
        raise ConfigError("APP_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    from cryptography.fernet import InvalidToken

    from core.config import ConfigError

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ConfigError("Stored API key cannot be decrypted (APP_ENCRYPTION_KEY changed?).") from exc
