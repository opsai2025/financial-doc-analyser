"""Persian display helpers: digits, Jalali dates, Tehran time."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30), "IRST")  # Iran has no DST since 2022

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)


def fa_digits(value: object) -> str:
    return str(value).translate(_FA_DIGITS)


def fa_number(value: int | float) -> str:
    return fa_digits(f"{value:,}").replace(",", "٬")


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    for gm, length in enumerate((31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31), start=1):
        if gd <= length:
            return gy, gm, gd
        gd -= length
    raise ValueError("invalid Jalali date")


_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_jalali_date(text: str) -> date | None:
    """'۱۴۰۵/۰۷/۱۷' or '1405-7-17' -> Gregorian date; None if empty or invalid."""
    parts = text.strip().translate(_TO_LATIN).replace("-", "/").split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    jy, jm, jd = map(int, parts)
    if not (1 <= jm <= 12 and 1 <= jd <= (31 if jm <= 6 else 30) and 1300 <= jy <= 1500):
        return None
    try:
        result = date(*jalali_to_gregorian(jy, jm, jd))
    except ValueError:
        return None
    return result if gregorian_to_jalali(result.year, result.month, result.day) == (jy, jm, jd) else None


def jalali_date(value: date | datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = to_tehran(value).date()
    jy, jm, jd = gregorian_to_jalali(value.year, value.month, value.day)
    return fa_digits(f"{jy}/{jm:02d}/{jd:02d}")


def jalali_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    local = to_tehran(value)
    return f"{jalali_date(local.date())} {fa_digits(local.strftime('%H:%M'))}"


def to_tehran(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(TEHRAN_TZ)
