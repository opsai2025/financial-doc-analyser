from datetime import UTC, date, datetime

from core.fa import fa_number, gregorian_to_jalali, jalali_date, jalali_datetime
from core.security import (
    generate_temp_password,
    hash_password,
    password_policy_errors,
    read_session,
    sign_session,
    verify_password,
)


def test_jalali_known_dates():
    assert gregorian_to_jalali(2025, 3, 21) == (1404, 1, 1)
    assert gregorian_to_jalali(2026, 10, 7) == (1405, 7, 15)
    assert gregorian_to_jalali(2024, 3, 19) == (1402, 12, 29)
    assert gregorian_to_jalali(2024, 3, 20) == (1403, 1, 1)
    assert jalali_date(date(2025, 3, 21)) == "۱۴۰۴/۰۱/۰۱"


def test_jalali_datetime_uses_tehran_time():
    # 21:00 UTC on 2025-03-20 is 00:30 on 1404/01/01 in Tehran
    assert jalali_datetime(datetime(2025, 3, 20, 21, 0, tzinfo=UTC)) == "۱۴۰۴/۰۱/۰۱ ۰۰:۳۰"
    assert jalali_date(None) == "—"


def test_fa_number():
    assert fa_number(12345) == "۱۲٬۳۴۵"


def test_password_hash_roundtrip_including_long_persian():
    long_pwd = "رمز" * 40 + "1"  # > 72 bytes in UTF-8
    h = hash_password(long_pwd)
    assert verify_password(long_pwd, h)
    assert not verify_password(long_pwd + "x", h)
    assert not verify_password("anything", "not-a-hash")


def test_password_policy():
    assert password_policy_errors("abc12345") == []
    assert password_policy_errors("رمزعبور۱۲") == []
    assert password_policy_errors("short1")
    assert password_policy_errors("onlyletters")
    assert password_policy_errors("12345678")
    assert password_policy_errors(" abc12345")


def test_temp_password_meets_policy():
    for _ in range(50):
        assert password_policy_errors(generate_temp_password()) == []


def test_session_token():
    secret = "x" * 40
    token = sign_session(secret, 7, 3)
    assert read_session(secret, token, 60) == (7, 3)
    assert read_session("y" * 40, token, 60) is None
    assert read_session(secret, token + "tampered", 60) is None
