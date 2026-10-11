# Refactor v2 — Plan & Status

Decisions referenced as `D-x.y` live in [DECISIONS.md](DECISIONS.md). Problems of the v1 code are listed in [LEGACY_ANALYSIS.md](LEGACY_ANALYSIS.md).

## Status

| Step | Title | Status |
|---|---|---|
| 1 | Foundation, design system, users | ✅ approved & committed |
| 2a | Providers/keys admin, access grants, engine, persistence | ✅ approved & committed (feedback round: DECISIONS §11, D-11.1–D-11.18) |
| 2b | Upload UI + version picker, history, guest results, Excel, usage bars | ✅ committed (DECISIONS §12, D-12.1–D-12.16); owner's test points deferred to the final review round |
| 3 | Results dashboard | ✅ committed with the recommended answers (DECISIONS §13); round 1 refinements committed (DECISIONS §14) |
| 4 | Deploy-readiness (reduced, D-15.1); exports/settings polish deferred | ✅ committed (DECISIONS §15); deployment on Posit pending (owner) |

Workflow per step: decision session (explain current behaviour first, visually) → implement → commit → owner tests → refinements (D-0.1, D-0.5).

## ▶ Where we are (handoff, 2026-10-11)
**State:** Steps 1–3, round 1 and a reduced Step 4 (deploy-readiness, DECISIONS §15) are committed on `refactor/v2` and published to the owner's repo as one snapshot commit on `main` (D-15.7; full history only in the local `refactor/v2`). The owner accepted all built features without testing (detailed checks later, in production) and wants the app on **Posit Connect Cloud (free plan)** as soon as possible.

**Verified:** 136 tests on SQLite and on Postgres 17 (`TEST_DATABASE_URL`, Docker), including migrations → exact model schema on Postgres; a scratch server on Postgres in Chrome (login + refresh, logout, a running request with the keep-open hint, session-only login without cookie routes).
**Not verified yet:** the Supabase Storage adapter against real Supabase (only mocked) and the upload size limit of Posit's proxy. Posit does **not** launch `app.py` as `st.App` (D-15.8): login there lasts per browser tab; persistent login is an open question (D-15.8).

**Next steps, in order:**
1. **Owner deploys** (docs/SETUP.md §7): `main` is pushed to the owner's repo (D-15.7); Supabase prod project; new Posit content from that repo with the Variables. Then the first checks in §7.4. Problems come back as a batch.
2. Optional before that: dev Supabase values in the local `.env` → run the app locally against Supabase (Storage upload/download/delete).
3. **Deferred backlog** (D-15.1): Word/PDF report, Excel RTL polish, admin-editable defaults and usage overview vs. Supabase limits; then the final review round (items below).

**Open items for the review round** (raised, not decided):
- Viewers land on History after login (the Dashboard was empty when this was set); confirm with the owner.
- Data grids are LTR canvases (D-9.9): wide tables can cut the right-hand (first) columns. Fixed pixel widths are used in the document table; other tables may need the same.
- Explanations in the document table are cut to the column width (full text on hover).
- Per-attempt times exist only for documents analysed after migration 0009.

**How to check the UI** (never on the owner's data): `scripts/seed_scratch.py` (SQLite or the Docker Postgres) fills a scratch database and `scripts/ui_screenshots.py` takes Chrome screenshots of the scratch server on port 8511. Usage is in each script's docstring and in CLAUDE.md. `tests/test_pages_smoke.py` runs every page for every role through `streamlit_app.py` (AppTest), including open History rows and providers.

---

## Step 1 — Foundation, design system, users
- Move v1 to `legacy/app_v1.py`; new modular structure (below); `app.py` = entry.
- Config from `.env` (`core/config.py`), SQLAlchemy engine (Supabase Postgres or local SQLite fallback).
- Design system (`ui/theme.py`, `ui/components.py`): RTL, right sidebar, header, Vazirmatn, Material icons, buttons, help boxes, status cards (D-9.*).
- Auth: login, bcrypt, lockout, signed session cookie, forced password change, bootstrap admin (D-4.1, D-4.2, D-4.7).
- Admin → User management: list + status cards, create, edit role/quotas/active, reset password, guest cap + expiry; admin cannot deactivate/demote self.
- Profile: change password, view quotas (usage bars wired in Step 2).
- Sidebar modules with placeholders for later steps.
- Persian normalization utility (`core/text_normalize.py`, D-7.3/7.4) + unit tests.

## Step 2 — Providers, analysis engine, persistence
- Admin: providers / models / encrypted API keys, role defaults + per-user grants (D-6.*). User: private providers.
- LLM adapters: Gemini native, OpenAI-compatible, Anthropic (D-6.2) using frozen prompt spec (D-2.*).
- Rate limiting per key/model (RPM/TPM/RPD), key rotation within a request, retries only for transient errors.
- Background job runner; per-document isolation and status (D-5.4); quotas (D-4.4, D-4.5, D-5.5); duplicates (D-5.3); cache/version picker (D-5.6).
- Persistence: requests, documents, results (linked sheet tables + gzip JSON), usage log, Supabase Storage for PDFs; guest in-memory results (D-4.3).
- History module; integrated Excel export with status sheet (D-8.*) — basic styling.

## Step 3 — Results dashboard
- Cards → charts → tables (D-9.6); v1's 7 charts rebuilt (Plotly, Persian labels, Vazirmatn).
- Company grouping using Level-B keys (D-7.4); persisted merges; request sharing with viewers (D-4.3).

## Step 4 — Exports, settings, hardening
- RTL-styled Excel (+ Word/PDF report if agreed in the Step 4 session).
- Admin system settings (defaults, usage overview vs. Supabase limits).
- HTML escaping everywhere, ZIP safety limits, tests, README/SETUP, remove `legacy/`.

---

## Structure (✅ = exists)
```
app.py                    ✅ entry: st.App launcher + /auth/session, /auth/logout routes
streamlit_app.py          ✅ UI script: page config, theme, auth gate, role-based navigation, sidebar
.streamlit/config.toml    ✅ theme (colors, Vazirmatn font face, sidebar), static serving
static/                   ✅ fonts/Vazirmatn-Variable.woff2 (+OFL), icons/*.svg
core/config.py            ✅ settings from .env, default quotas
core/db.py                ✅ engine/session (Postgres or SQLite), create_schema()
core/models.py            ✅ SQLAlchemy models (users; more in Step 2)
core/security.py          ✅ bcrypt (sha256 pre-hash), password policy, signed session tokens
core/text_normalize.py    ✅ Persian normalization Level A/B (D-7)
core/fa.py                ✅ Persian digits, Jalali dates, Tehran time
core/prompt_spec.py       ✅ FROZEN prompt/schema/config, generated from v1 + fingerprint (D-2)
core/sheets.py            ✅ the six v1 sheets: table names, Persian↔DB column mapping
core/migrate.py           ✅ Alembic upgrade at startup (stamps Step-1 databases; SQLite FKs off while migrating)
core/logging_setup.py     ✅ console + data/logs/app.log (rotating; LOG_FILE overrides)
migrations/               ✅ 0001_users … 0006_raw_outputs, 0007_request_shares, 0008_company_merges, 0009_times_tokens_rerun
services/auth.py          ✅ login + lockout, session tokens, DB login tickets, password change, bootstrap admin
services/auth_routes.py   ✅ ASGI cookie routes
services/users.py         ✅ user CRUD, role labels, quota defaults, guards
services/providers.py     ✅ providers/models/encrypted keys, access grants + resolution, test connection
services/llm/             ✅ base (errors/classification), gemini, openai_compat, claude, mock
services/jobs.py          ✅ JobManager: planning, spooling, persistence, results, retry, recovery
services/scheduler.py     ✅ dispatch scheduler: key x model capacity (concurrency, RPM, RPD), cooldowns, server cap, turns, retries (docs/ENGINE.md)
services/app_settings.py  ✅ admin-editable server settings (server-wide cap)
services/quota.py         ✅ document quotas, DB/storage capacity
services/uploads.py       ✅ PDF / ZIP intake with limits
services/results.py       ✅ JSON → v1 sheet rows, normalization, year, save/load
services/storage.py       ✅ Supabase Storage (REST) / local folder
services/excel.py         ✅ integrated workbook: status sheet + v1 sheets, RTL, Excel-only adjustments (D-12.9–13)
services/history.py       ✅ request search, deletion, kept PDFs, sharing with viewers (D-12.2–6)
services/dashboard.py     ✅ dashboard data: sources, duplicates, company keys and merges, chart data (D-13)
ui/provider_editor.py     ✅ provider/model/key editor (settings + profile)
ui/charts.py              ✅ Plotly figures of the dashboard + chart explanations (D-13.7)
ui/request_view.py        ✅ one request's view (status, Excel, retry, re-upload, share, delete), used by analysis + history
scripts/check_provider.py ✅ one real analysis with TEST_* settings, schema check
scripts/seed_scratch.py   ✅ scratch database for UI checks (users, mock provider, multi-year requests)
scripts/ui_screenshots.py ✅ Chrome screenshots of the scratch server (Playwright)
ui/styles.css             ✅ RTL, right sidebar, header, buttons, help boxes, card tones
ui/components.py          ✅ header, page title, help_box, cards_row, md_escape, redirect, usage_bar, file_label
ui/session.py             ✅ session-state login handling
ui/auth_views.py          ✅ login view, forced password change view
ui/forms.py               ✅ password change form
app_pages/                ✅ analysis (version picker), history (requests, kept files, shared), settings (providers, access, engine status, server cap), users (+ usage), profile (+ usage bars, shared and own providers), dashboard (overview, company, merges)
tests/                    ✅ normalization, Jalali/security, users/auth services
legacy/app_v1.py          removed in Step 4 (D-15.1); only in the local branch refactor/v2
requirements.txt          ✅ runtime only (Posit installs it); requirements-dev.txt adds pytest + playwright
```

## Data model (draft — finalised in Step 2 session)
| Table | Key | Content |
|---|---|---|
| users | id | username, display_name, password_hash, role, is_active, must_change_password, docs_per_hour, docs_per_day, db_quota_mb, storage_quota_mb, guest_doc_cap, expires_at, failed_logins, locked_until, created_by, timestamps |
| providers | id | owner_user_id (NULL = admin/global), name, api_type, base_url, is_active |
| models | id | provider_id, model_name, display_name, supports_pdf, rpm, rpd, is_active |
| api_keys | id | provider_id, label, key_encrypted, key_last4, is_active |
| role_grants / user_grants | id | role or user, provider_id, model_id (NULL = all), api_key_id (NULL = all), effect (allow/deny for user overrides) |
| key_usage_daily | (api_key_id, date) | requests, tokens |
| requests | request_id | user_id, provider/model, selected keys, status, counts, created_at |
| documents | document_id (SHA-256) | size_bytes, storage_path (NULL if not kept) |
| request_documents | (request_id, document_id) | file_name, status, reason, result_ref (request_id that produced the result), source (new/reused) |
| results | (request_id, document_id) | provider, model, created_at, raw_json_gz, size_bytes |
| sheet tables ×6 | (request_id, document_id, row_no) | v1 sheet columns as text (Level-A normalized) |
| usage_log | id | user_id, at, doc_count, own_key flag |
| request_shares | (request_id, viewer_id) | shared_by, at |
