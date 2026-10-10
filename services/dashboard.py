"""Results dashboard data (D-13): which documents count, company keys and merges, chart-ready data.

Pure functions over `DocRecord`s, so the rules can be tested without Streamlit:
* source: all results the user may see, or one request (D-13.1);
* one result per PDF (newest, D-13.2) and one document per company and year (D-13.3);
* company key = Level-B key without legal-form words (D-13.4) plus the user's saved merges (D-13.5).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select

from core.db import session_scope
from core.models import (
    SHEET_TABLES,
    AnalysisRequest,
    AnalysisResult,
    CompanyMerge,
    RequestDocument,
    RequestShare,
    Role,
    as_utc,
)
from core.prompt_spec import RESPONSE_SCHEMA
from core.text_normalize import comparison_key
from services import results
from services.auth import CurrentUser
from services.jobs import REUSED, SUCCEEDED, RequestState

log = logging.getLogger(__name__)

SHEETS_USED = ("summary", "emphasis", "legal", "checklist", "basis")

# Ordinal scales of the frozen schema (D-13.11)
OPINIONS = ("مقبول", "مشروط", "مردود", "عدم اظهارنظر")
RISKS = ("پایین", "متوسط", "بالا", "بحرانی")
STATUSES = ("مصداق ندارد", "بررسی شده - ریسک خاصی گزارش نشده", "مسئله کلیدی منجر به اظهارنظر مشروط", "ریسک بحرانی")
_REPORT = RESPONSE_SCHEMA["properties"][results.ROOT]["properties"]
ALL_TOPICS: tuple[str, ...] = tuple(_REPORT[results.PART3]["items"]["properties"]["موضوع"]["enum"])
_PART2 = _REPORT[results.PART2]["properties"]
# Main categories in schema order, for stable chart colours (D-13.7)
RISK_CATEGORIES: tuple[str, ...] = tuple(
    _PART2["بند_تاکید_بر_مطالب_خاص"]["properties"]["موارد_مطرح_شده"]["items"]["properties"]["دسته_اصلی_ریسک"]["enum"])
LEGAL_CATEGORIES: tuple[str, ...] = tuple(
    _PART2["گزارش_رعایت_الزامات_قانونی"]["properties"]["تخلفات"]["items"]["properties"]["دسته_اصلی"]["enum"])
SIGNATURE_TOPIC = "صفحه امضا های سازمان حسابرسی"
# v1's heatmap order (plot_checklist_heatmap); the other topics follow in schema order (D-13.8)
HEATMAP_PRIORITY = (
    "کفایت سرمایه", "نسبت‌ها در چارچوب بازل", "ریسک نقدینگی", "مدیریت دارایی و بدهی (ALM)", "ریسک نرخ بهره",
    "تمرکز ریسک اعتباری", "ذخیره‌گیری (کلی)", "صورت جریان وجوه نقد", "کنترل‌های داخلی و حسابرسی داخلی",
    "حاکمیت شرکتی", "اوراق بهادار و سرمایه‌گذاری‌ها", "تسعیر ارز و عملیات خارجی",
    "تعهدات ارزی و اختلاف با بانک مرکزی", "ذخیره مطالبات مشکوک‌الوصول", "مطالبات مشکوک‌الوصول",
    "تسهیلات و اعتبارات", "سرمایه‌گذاری در شرکت‌های وابسته", "کاهش ارزش دارایی‌ها", "افشای ریسک‌های عملیاتی",
    "نسبت‌های بدهی و نقدینگی", "نسبت کفایت سرمایه", "معاملات با اشخاص وابسته", "تداوم فعالیت",
    "انطباق با مقررات ضدپولشویی (AML/CFT)",
)
LEGAL_FORMS = ("با مسئولیت محدود", "سهامی عام", "سهامی خاص", "شرکت")
_LEGAL_KEYS = tuple(comparison_key(f) for f in LEGAL_FORMS)
_EPOCH = datetime.min.replace(tzinfo=UTC)


def company_key(name: object) -> str:
    """Level-B key without legal-form words (D-13.4); '' if the name is empty."""
    key = f" {comparison_key(name or '')} "
    for form in _LEGAL_KEYS:
        while f" {form} " in key:
            key = key.replace(f" {form} ", " ")
    return " ".join(key.split())


def ordinal(value: object, scale: tuple[str, ...]) -> int | None:
    """Position of an enum value on its scale, matched on Level-B keys; None if outside the schema."""
    key = comparison_key(value or "")
    return next((i for i, option in enumerate(scale) if comparison_key(option) == key), None) if key else None


def applicable(row: dict) -> bool:
    return str(row.get("is_applicable")) == "True"


@dataclass
class DocRecord:
    """One analysed document as the dashboard sees it."""

    request_id: str  # the request it was found in
    result_request_id: str | None  # where the stored result lives (None = in memory only)
    document_id: str
    file_name: str
    analysed_at: datetime | None
    model_label: str
    rows: dict[str, list[dict]] = field(default_factory=dict)  # sheet key -> rows (db column names)

    @property
    def summary(self) -> dict:
        return (self.rows.get("summary") or [{}])[0]

    @property
    def company(self) -> str:
        return self.summary.get("company_name") or "نامشخص"

    @property
    def key(self) -> str:
        return company_key(self.summary.get("company_name")) or f"?{self.document_id[:12]}"

    @property
    def year(self) -> int | None:
        return self.summary.get("year")


# ---------------------------------------------------------------- loading (D-13.1, D-13.2)
def _visible_requests(user: CurrentUser, shared: bool = False):
    """Own requests (admins: all), or the requests shared with the user (viewers: always, D-14.8)."""
    query = select(AnalysisRequest.id)
    if shared or user.role is Role.VIEWER:
        return query.join(RequestShare, RequestShare.request_id == AnalysisRequest.id).where(
            RequestShare.user_id == user.id)
    if not user.is_admin:
        return query.where(AnalysisRequest.user_id == user.id)
    return query


def _load_rows(db, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, list[dict]]]:
    found: dict[tuple[str, str], dict[str, list[dict]]] = {pair: {k: [] for k in SHEETS_USED} for pair in pairs}
    if not pairs:
        return found
    request_ids = {p[0] for p in pairs}
    for key in SHEETS_USED:
        table = SHEET_TABLES[key]
        for row in db.execute(select(table).where(table.c.request_id.in_(request_ids)).order_by(table.c.row_no)):
            data = dict(row._mapping)
            pair = (data["request_id"], data["document_id"])
            if pair in found:
                found[pair][key].append(data)
    return found


def load_all(user: CurrentUser, shared: bool = False) -> list[DocRecord]:
    """Stored results of the user's requests (or those shared with them); the newest result per PDF (D-13.2)."""
    with session_scope() as db:
        candidates = db.execute(
            select(RequestDocument.request_id, RequestDocument.document_id, RequestDocument.file_name,
                   RequestDocument.result_request_id)
            .where(RequestDocument.request_id.in_(_visible_requests(user, shared)),
                   RequestDocument.status.in_([SUCCEEDED, REUSED]), RequestDocument.stored.is_(True),
                   RequestDocument.result_request_id.is_not(None))).all()
        result_ids = {c.result_request_id for c in candidates}
        meta = {(r.request_id, r.document_id): r for r in db.scalars(
            select(AnalysisResult).where(AnalysisResult.request_id.in_(result_ids)))} if result_ids else {}
        newest: dict[str, tuple] = {}
        for c in candidates:
            result = meta.get((c.result_request_id, c.document_id))
            if result is None:
                continue
            current = newest.get(c.document_id)
            if current is None or as_utc(result.created_at) > as_utc(current[1].created_at):
                newest[c.document_id] = (c, result)
        rows = _load_rows(db, {(r.request_id, r.document_id) for _, r in newest.values()})
        return [DocRecord(c.request_id, r.request_id, r.document_id, c.file_name, as_utc(r.created_at),
                          f"{r.provider_name} › {r.model_name}", rows[(r.request_id, r.document_id)])
                for c, r in newest.values()]


def load_request(state: RequestState) -> list[DocRecord]:
    """The documents of one request with a result: stored ones from the DB, others from memory."""
    docs = [d for d in state.docs if d.status in (SUCCEEDED, REUSED)]
    stored = {(d.result_request_id, d.document_id) for d in docs if d.stored and d.result_request_id}
    with session_scope() as db:
        rows = _load_rows(db, stored)
        meta = {(r.request_id, r.document_id): r for r in db.scalars(
            select(AnalysisResult).where(AnalysisResult.request_id.in_({p[0] for p in stored})))} if stored else {}
    records = []
    for doc in docs:
        pair = (doc.result_request_id, doc.document_id)
        if pair in rows and pair in meta:
            result = meta[pair]
            records.append(DocRecord(state.id, result.request_id, doc.document_id, doc.file_name,
                                     as_utc(result.created_at), f"{result.provider_name} › {result.model_name}",
                                     rows[pair]))
        elif doc.data is not None:
            prepared = results.prepare(doc.data).rows
            records.append(DocRecord(state.id, None, doc.document_id, doc.file_name, doc.finished_at,
                                     state.model_label, {k: prepared.get(k, []) for k in SHEETS_USED}))
    return records


# ---------------------------------------------------------------- companies and merges (D-13.4, D-13.5)
def merges(user_id: int) -> dict[str, str]:
    """member key -> main key."""
    with session_scope() as db:
        return {m.member_key: m.main_key for m in db.scalars(select(CompanyMerge).where(CompanyMerge.user_id == user_id))}


def save_merge(user_id: int, main_key: str, member_keys: list[str]) -> None:
    """Show `member_keys` as `main_key`. Groups never chain: members of a merged-away main follow it."""
    members = {k for k in member_keys if k and k != main_key}
    if not main_key or not members:
        return
    with session_scope() as db:
        existing = {m.member_key: m for m in db.scalars(select(CompanyMerge).where(CompanyMerge.user_id == user_id))}
        if main_key in existing:  # the new main was itself merged away: it becomes a main again
            db.delete(existing.pop(main_key))
        for m in existing.values():
            if m.main_key in members:  # a group whose main is merged away moves along
                m.main_key = main_key
        for key in members:
            if key in existing:
                existing[key].main_key = main_key
            else:
                db.add(CompanyMerge(user_id=user_id, member_key=key, main_key=main_key))
    log.info("Company merge saved for user %d: %d name(s) -> one company", user_id, len(members))


def remove_group(user_id: int, main_key: str) -> None:
    with session_scope() as db:
        db.execute(delete(CompanyMerge).where(CompanyMerge.user_id == user_id, CompanyMerge.main_key == main_key))


@dataclass
class Company:
    key: str  # group key (the main key)
    name: str
    records: list[DocRecord] = field(default_factory=list)  # all documents of the group (before D-13.3)
    member_keys: set[str] = field(default_factory=set)

    @property
    def years(self) -> list[int]:
        return sorted({r.year for r in self.records if r.year is not None})


def group_companies(records: list[DocRecord], merge_map: dict[str, str]) -> dict[str, Company]:
    companies: dict[str, Company] = {}
    for record in records:
        group = merge_map.get(record.key, record.key)
        company = companies.setdefault(group, Company(group, ""))
        company.records.append(record)
        company.member_keys.add(record.key)
    for company in companies.values():
        own = [r.company for r in company.records if r.key == company.key] or [r.company for r in company.records]
        company.name = Counter(own).most_common(1)[0][0]
    return companies


def group_of(merge_map: dict[str, str]):
    return lambda record: merge_map.get(record.key, record.key)


# ---------------------------------------------------------------- one document per company and year (D-13.3)
def choose_per_year(records: list[DocRecord], group, overrides: dict[tuple[str, int], str] | None = None
                    ) -> tuple[list[DocRecord], dict[tuple[str, int], list[DocRecord]]]:
    """(documents that count, {(group, year): all candidates, chosen first} for years with several documents).

    Default: the most recently analysed document of the year; `overrides` picks another by document id.
    Documents without a year are kept (they can't be placed on a year axis).
    """
    overrides = overrides or {}
    buckets: dict[tuple[str, int], list[DocRecord]] = {}
    chosen = []
    for record in records:
        if record.year is None:
            chosen.append(record)
        else:
            buckets.setdefault((group(record), record.year), []).append(record)
    alternates = {}
    for bucket_key, bucket in buckets.items():
        bucket.sort(key=lambda r: r.analysed_at or _EPOCH, reverse=True)  # newest first
        pick = next((r for r in bucket if r.document_id == overrides.get(bucket_key)), bucket[0])
        chosen.append(pick)
        if len(bucket) > 1:
            alternates[bucket_key] = [pick, *(r for r in bucket if r is not pick)]
    return chosen, alternates


# ---------------------------------------------------------------- chart data
def trend_points(records: list[DocRecord], column: str, scale: tuple[str, ...]) -> list[tuple[int, int]]:
    """(year, position on the scale) per document with both known."""
    points = []
    for record in records:
        position = ordinal(record.summary.get(column), scale)
        if record.year is not None and position is not None:
            points.append((record.year, position))
    return sorted(points)


def items(records: list[DocRecord], sheet: str, *, only_applicable: bool = True) -> list[dict]:
    """Item rows of a sheet with the document's year, company and file (applicable items only by default)."""
    found = []
    for record in records:
        for row in record.rows.get(sheet, []):
            if only_applicable and sheet in ("emphasis", "legal", "basis") and not applicable(row):
                continue
            found.append({**row, "year": record.year, "_company": record.company, "_file": record.file_name})
    return found


def heatmap(records: list[DocRecord], show_all: bool = False) -> tuple[list[str], list[int], list[list[int]]]:
    """(topics, years, worst status per topic × year; missing = «مصداق ندارد»), D-13.8."""
    worst: dict[tuple[str, int], int] = {}
    seen_topics: dict[str, str] = {}
    for record in records:
        if record.year is None:
            continue
        for row in record.rows.get("checklist", []):
            topic, status = row.get("topic"), ordinal(row.get("status"), STATUSES)
            if not topic or status is None:
                continue
            key = comparison_key(topic)
            seen_topics.setdefault(key, topic)
            worst[(key, record.year)] = max(worst.get((key, record.year), 0), status)
    years = sorted({r.year for r in records if r.year is not None})
    ordered = [*HEATMAP_PRIORITY, *(t for t in ALL_TOPICS if t not in HEATMAP_PRIORITY)]
    ordered += [t for k, t in seen_topics.items() if k not in {comparison_key(o) for o in ordered}]
    reported = {k for (k, _), v in worst.items() if v > 0}
    topics = [t for t in ordered if comparison_key(t) != comparison_key(SIGNATURE_TOPIC)
              and (show_all or comparison_key(t) in reported)]
    matrix = [[worst.get((comparison_key(t), y), 0) for y in years] for t in topics]
    return topics, years, matrix


def distribution(records: list[DocRecord], column: str, scale: tuple[str, ...]) -> dict[str, int]:
    counts = {option: 0 for option in scale}
    for record in records:
        position = ordinal(record.summary.get(column), scale)
        if position is not None:
            counts[scale[position]] += 1
    return counts
