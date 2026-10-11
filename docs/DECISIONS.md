# Decision Log — Refactor v2

All design decisions agreed with the product owner. **Source of truth** for the refactor.
Change a decision only after discussing it with the owner, and record the change here (date + reason).

Legend: ✅ decided · ❓ open

---

## 0. Working agreement
- ✅ D-0.1 Before each step: a decision session to resolve every ambiguity. After each step: a feedback/change round. No step starts on assumed answers.
- ✅ D-0.2 Git: branch `refactor/v2`. First commit = owner's uncommitted baseline change (model names in `app.py`). One commit per **approved** step. Legacy app kept as `legacy/app_v1.py` until Step 4, then removed. `app.py` remains the entry point.
- ✅ D-0.3 Helper docs (`CLAUDE.md`, `docs/*`) are kept up to date so any future session can continue without the chat history.
- ✅ D-0.4 All user-facing text in Persian; code identifiers, comments and logs in English.
- ✅ D-0.5 (owner, 2026-10-09) From Step 2b on, steps are committed without a per-step test round: the owner first completes one full pass of the redesign (Steps 3–4), then reviews missed features and problems in one round. The decision session before each step stays.

## 1. Purpose & scope
- ✅ D-1.1 The app analyses Iranian **bank** audit reports (independent auditor & statutory inspector reports, PDF) with an LLM and produces structured results, dashboards and Excel. Mostly banks (checklist is bank-specific: Basel, CBI, AML…).
- ✅ D-1.2 No hard-coded LLM provider (AvalAI was only hard-coded in v1). Providers/models/keys are configured in the app (see §6).

## 2. Prompts & structured output — FROZEN
- ✅ D-2.1 The prompt text, the system instruction (`شما تحلیلگر مالی هستید.`), the JSON response schema (all property names, enums, descriptions) and generation config (`temperature: 0.5`, JSON output) must **never** be changed.
- ✅ D-2.2 They are moved verbatim into `core/prompt_spec.py`; a test asserts their hash equals the v1 original (`legacy/app_v1.py`).
- ✅ D-2.3 Only the transport adapts per API type (how the PDF and schema are passed); the content sent is identical.
- ✅ D-2.4 (2a) Each model has a **"supports temperature"** switch (default on). On: temperature 0.5 is sent exactly as in v1. Off (models that reject sampling parameters, e.g. newest Claude, OpenAI reasoning models): temperature is omitted for that model only, and every result records whether it was applied.
- ✅ D-2.5 (2a) The Anthropic adapter sends a copy of the frozen schema with `additionalProperties: false` added to every object (required by Claude structured outputs). Names, types, enums, descriptions and required lists are unchanged; other providers receive the schema exactly as in v1.

## 3. Infrastructure & storage
- ✅ D-3.1 Database: **Supabase** (free tier: 500 MB DB + 1 GB Storage per project, 2 projects). Project 1 = production, project 2 = development. Access via SQLAlchemy (Postgres through the Supabase *Session pooler*).
- ✅ D-3.2 Until Supabase exists, development falls back to local SQLite (`DATABASE_URL` empty) and local folder storage. Same code path.
- ✅ D-3.3 Do **not** store uncritical data in Supabase (no logs, no generated Excel, no chart images).
- ✅ D-3.4 Stored: users/roles/quotas, providers/models/keys (keys encrypted), requests & per-document status, results (per-sheet linked tables + gzip raw JSON), usage log, request shares.
- ✅ D-3.5 Uploaded PDFs go to **Supabase Storage** (private bucket). Identical files (same SHA-256) are stored once.
- ⚠️ Note: free-tier projects pause after ~7 days of inactivity (resume from dashboard or keep-alive ping). Documented in `docs/SETUP.md`.

## 4. Users, roles, quotas
- ✅ D-4.1 Own `users` table, bcrypt hashes, **username** login. No public sign-up: admin creates accounts; user must change the temporary password at first login. Deactivate instead of delete.
- ✅ D-4.2 Start fresh: only a bootstrap admin from `.env`. Old `config.yaml` users and hard-coded passwords are dropped.
- ✅ D-4.3 Roles:
  - **Admin** — everything; unlimited quotas; sees all analyses.
  - **Analyst** — runs analyses; sees own analyses.
  - **Viewer** — cannot analyse; sees only requests explicitly **shared** with them (read-only: dashboard + Excel).
  - **Guest** — admin-created; **lifetime document cap + expiry date**; nothing stored in DB/Storage (results kept in server memory ~2 h, Excel download only). Only a tiny usage row is stored to enforce the cap.
- ✅ D-4.4 Analyst quotas (per user, set by admin, global defaults): documents/hour and documents/day. Only **new API analyses** count (cache reuse and failures are free).
- ✅ D-4.5 Capacity: two MB quotas per non-guest user — **results DB** and **PDF storage**. When full, never block: results full → analysis runs, Excel-only output; PDF storage full → analysis runs, PDF not kept. User warned before starting.
- ✅ D-4.6 Visibility: own analyses only; admin sees all; viewers see shared requests.
- ✅ D-4.7 Defaults (changeable by admin): analyst 10 docs/h, 50 docs/day, 100 MB storage; password ≥ 8 chars with letters+digits; lock 15 min after 5 failed logins; session 8 h.
- ✅ D-4.8 Guests using their own API keys still have the lifetime document cap.
- ✅ D-4.9 (Step 1) Login session = signed token (itsdangerous, `APP_SECRET_KEY`) in an **HttpOnly** cookie `fda_session` (SameSite=Lax, Secure on HTTPS), valid `SESSION_HOURS`. Streamlit cannot set cookies, so after login a single-use 60-second ticket redirects the browser to the ASGI route `/auth/session` (`app.py` → `st.App`). `users.session_version` revokes all sessions on password change/reset, deactivation or role change. Logout = `/auth/logout` clears the cookie.
- ✅ D-4.11 (owner, Step 1 feedback) Guests can see the results of their **last request (batch)** — all its successful documents (cards, tables, Excel) — kept in server memory, tied to the guest account, for `GUEST_RESULT_TTL_MINUTES` (120): a refresh or re-login within that time still shows them; a new request replaces them; lost on server restart.
- ✅ D-4.10 (Step 1) Guest validity is set in days when creating; when editing, the expiry is kept unless the admin ticks "extend" and enters new days. The last active admin cannot be demoted/deactivated; admins cannot demote/deactivate themselves.

## 5. Requests, keys, batches, cache
- ✅ D-5.1 `request_id` — one per analysis request (single file or batch), readable, e.g. `R-20261007-K3Q9`.
- ✅ D-5.2 `document_id` — SHA-256 of the file bytes. A stored result is unique by **(request_id, document_id)**; the same file can be re-analysed and each result is kept.
- ✅ D-5.3 Duplicate files (same hash) inside one batch are analysed once; duplicates reported as "duplicate – ignored".
- ✅ D-5.4 Fault isolation: failure of one document never disrupts the others; output contains all successful documents; per-document status reported (success / success with warnings / failed + reason / duplicate / skipped – quota / reused from previous result).
- ✅ D-5.5 Over quota: process what fits, rest "skipped – quota"; warning before start. "Retry failed/skipped" adds results to the **same** request.
- ✅ D-5.6 Cache = the user's **own** previous results (admin: everyone's). Per previously-analysed file the user sees all versions (datetime, model, provider). Default selection: most recent version with the same model+provider; if none → **new analysis**. Exactly one version selectable per file; re-analysing all/some files is one action. Reuse references the old result (no duplicated rows).
- ✅ D-5.7 Each request uses exactly **one model** and the API keys selected for it (rotation among selected keys).

## 6. Providers, models, API keys
- ✅ D-6.1 Admin panel configures providers: name, API type, base URL, API keys; and models under each provider.
- ✅ D-6.2 API types supported from the start: **Google Gemini (native)**, **OpenAI-compatible**, **Anthropic Claude**. Custom base URL allowed (gateways such as AvalAI).
- ✅ D-6.3 Access to admin-defined providers/models/keys: **role defaults + per-user overrides**.
- ✅ D-6.4 Every user (including guests) may define **private** providers/models/keys. Admin sees name/type/models but never key values. Own-key analyses don't count toward docs/hour/day quotas; DB/storage quotas still apply. Guest-owned providers/keys are deleted when the guest expires.
- ✅ D-6.5 (default) Only models flagged "accepts PDF input" are offered (no text-extraction fallback, which would change the model input).
- ✅ D-6.6 (default) API keys encrypted at rest (Fernet, `APP_ENCRYPTION_KEY`), shown masked (last 4 chars). Rate limits (RPM, TPM, RPD) configured per model/key, replacing v1 sidebar inputs. Per-key daily usage persisted.

## 7. Data cleansing & Persian normalization
- ✅ D-7.1 LLM-generated values are stored as **text**; only character-set normalization is applied. Fields produced by Python (ids, year, timestamps, status, counts) are typed.
- ✅ D-7.2 Raw model JSON is stored untouched (gzip); normalization applies to the per-sheet tables and Excel.
- ✅ D-7.3 **Level A** (stored/displayed text):
  1. `ي` U+064A, `ى` U+0649 → `ی` U+06CC; `ك` U+0643 → `ک` U+06A9
  2. Arabic presentation forms (U+FB50–U+FDFF, U+FE70–U+FEFF) → base letters (e.g. `ﻻ`→`لا`, `﷼`→`ریال`)
  3. Arabic-Indic digits `٠-٩` → Persian `۰-۹` (Latin digits unchanged)
  4. Remove kashida/tatweel U+0640
  5. Remove U+200B, U+FEFF, U+200E, U+200F, U+202A–U+202E, U+2066–U+2069; keep ZWNJ U+200C but collapse repeats and drop ZWNJ next to spaces or at text edges
  6. Unicode spaces (NBSP etc.) → space; collapse repeated spaces; trim
- ✅ D-7.4 **Level B** (comparison keys only — company grouping, enum matching, cache lookups; never displayed): Level A + remove diacritics (U+064B–U+0652, U+0654, U+0655, U+0670), `ة`→`ه`, `أ`/`إ`→`ا`, `ؤ`→`و`, all digits → Latin, ZWNJ & punctuation/symbols → space, lowercase Latin.
- ✅ D-7.5 **Level C** (orthographic rewriting: `می` separation, affix spacing, «» quotes, `٫` decimals) is **not** applied.
- References: Hazm normalizer (github.com/roshan-research/hazm), "Graphemic Normalization of the Perso-Arabic Script" (arXiv 2210.12273).

## 8. Excel export
- ✅ D-8.1 One integrated workbook per request (batch); no per-document files.
- ✅ D-8.2 Keep the v1 sheets and columns exactly (`بخش1_خلاصه`, `بند_اظهارنظر`, `بند_مبانی_اظهارنظر`, `بند_تاکید_بر_مطالب_خاص`, `گزارش_قانونی`, `بخش3_چک_لیست`), rows of all documents concatenated.
- ✅ D-8.3 New **first sheet**: status of every document in the request (keys, file name, full SHA-256, status, reason, model, provider, …).
- ✅ D-8.4 Every sheet gets `شناسه_درخواست` + `شناسه_سند` (first 12 hex chars of SHA-256); full hash only in the status sheet.

## 9. UI / UX (per `ui ux design rules.docx`)
- ✅ D-9.1 Fully RTL; sidebar on the **right**; navigation = sidebar module menu + a few tabs inside each module; admin-only items hidden for others.
- ✅ D-9.2 Header gradient `#0f172a → #1e293b`, title `#4ed793`, subtitle light grey, icon; header padding-top ≈ 2.5rem.
- ✅ D-9.3 Font Vazirmatn everywhere (incl. charts and Excel where possible); Material icons instead of emojis.
- ✅ D-9.4 Buttons: high contrast, border pattern from the rules (e.g. danger `#a43232` / border `#f65858` / white text).
- ✅ D-9.5 Help boxes: green (`#daf39e` / black) or blue (`rgba(28,131,255,.1)` / `#0054A3`); bullets for long guides.
- ✅ D-9.6 Result order: status cards → charts → tables → AI/text summary where relevant. Cards use conditional colors (risk/status).
- ✅ D-9.8 (Step 1) Dates shown in the Jalali calendar, Tehran time (UTC+03:30); numbers in cards/text use Persian digits, tables keep sortable values.
- ✅ D-9.10 (Step 1 feedback) RTL fixes in `ui/styles.css`: collapsed sidebar is clipped in place (Streamlit's LTR slide-out moved it into the page), sidebar resize handle disabled, top toolbar transparent (it hid the page header), collapse chevrons mirrored, Streamlit's English "Press Enter to apply" input hints hidden (they overlapped RTL text). Verified on desktop (1440px) and phone (420px) widths with repeated collapse/expand.
- ✅ D-9.9 (Step 1) Data grids (`st.dataframe`) are LTR-only canvases; pages reverse column order so the first column appears on the right.
- ✅ D-9.7 (default) Title «سامانه تحلیل هوشمند گزارش‌های حسابرسی», subtitle «تحلیل گزارش حسابرس مستقل و بازرس قانونی با هوش مصنوعی». Light content area, dark navy sidebar.

## 10. Step 2 — engine & persistence (decided 2026-10-08)
- ✅ D-10.1 Step 2 is split into **2a** (providers/models/keys admin + access grants + analysis engine + persistence, tested via a simple upload) and **2b** (full upload UI with version picker, history, guest results, integrated Excel, quota/usage bars). Each is tested and approved separately.
- ✅ D-10.2 Analyses run in **server-side background worker threads**; per-document status persisted in the DB; the page shows live progress and the browser may be closed. On server restart, unfinished documents become **"interrupted"** and can be retried with one click (re-upload requested if the PDF was not kept).
- ✅ D-10.3 Retries: default **3 attempts** total with backoff (~10 s, 30 s), switching to another selected key each attempt. Retry only transient errors (429, 5xx, timeouts, network, empty/invalid JSON). No retry on 400/401/403/404 or oversized file; a key failing with 401/403 is skipped for the rest of the request; 429 puts the key in a short cooldown. Admin can change attempts per model.
- ✅ D-10.4 Upload defaults (admin-changeable later): **20 MB per PDF**, **50 files per batch**, ZIP ≤ **300 MB** uncompressed; ZIP subfolders allowed, non-PDF entries ignored and listed; a model may declare a lower max file size.
- ~~D-10.5~~ Replaced by D-11.8 (TPM removed).
- ~~D-10.6~~ Replaced by D-11.9–D-11.11 (new scheduler, server cap 20, no per-request cap).
- ✅ D-10.7 Tools: **test-connection** button (tiny text-only request), **mock** API type (development mode only, returns sample structured output), **token usage** stored per analysed document.
- ✅ D-10.8 Key choice: user picks one model; all keys they may use for it are preselected (shown as label + last 4 chars) and can be unticked.
- ✅ D-10.9 Deletion: users can delete their own **requests** (frees DB quota; result rows still referenced by another request are kept until the last reference is gone) and their kept **PDFs** separately (results stay). Every delete asks for confirmation. Admin can delete anyone's.
- ✅ D-10.10 Results-DB usage = gzip raw-JSON bytes + UTF-8 bytes of the sheet-table values, recorded at save time; summed over the user's own new results (cache reuse costs 0). PDF-storage usage = sum of the sizes of the distinct PDFs the user keeps (physically stored once).
- ✅ D-10.11 Schema changes via **Alembic** migrations, applied automatically at startup (SQLite and Supabase).
- ✅ D-10.12 Users manage their private providers in a **"ارائه‌دهندگان من" tab in My profile** (analysts, guests; not viewers). Admin-defined providers and access grants live in System settings.
- ✅ D-10.13 2a/2b are built and tested on **local SQLite + local folder storage**; the Supabase Storage adapter is built and unit-tested with mocks; switching = filling `.env`.
- ✅ D-10.14 Test material: owner puts 2–3 sample PDFs in git-ignored `samples/` and a test key in `.env` (`TEST_PROVIDER_TYPE`, `TEST_BASE_URL`, `TEST_API_KEY`, `TEST_MODEL`); one real analysis verifies the output structure against v1; the mock provider covers everything else.
- ✅ D-10.15 (2a, default) Document quotas use **rolling windows**: the last 60 minutes and the last 24 hours. Documents already queued count as reserved.
- ✅ D-10.16 (2a) A hidden landing page at `/` forwards each role to its start page, so every module keeps a stable URL (`/analysis`, `/users`…) and Streamlit's English "page not found" dialog never appears for real pages.
- ✅ D-10.17 (2a) Request ids `R-<UTC date>-<4 chars>`. Uploaded PDFs are spooled to `data/spool/<request_id>/` for processing; spool files of successful documents are deleted immediately, others are kept up to 7 days for retries (or fetched back from Storage if kept). Per key × model request/token counters are kept per UTC day.
- ✅ D-10.18 (2a, verified) Real run: AvalAI OpenAI-compatible endpoint + `gemini-3.8-flash` on `samples/1398.pdf` — 122 s, 83.6k input / 10.1k output tokens, **0 schema violations**, year read correctly from Persian digits (v1 would have produced "Unknown"). Stored size ≈ 31 KB per document (gzip JSON ≈ 6 KB) → ≈ 16k documents fit in 500 MB.

## 11. Step 2a feedback round (decided 2026-10-08/09)
Owner's test report: login stops working after a while (Issue 1); a key with concurrency 5 processed documents one by one (Issue 2); show admin-shared providers in the profile (Req. 1); show the request creator to admins (Req. 2). Full engine analysis: [ENGINE.md](ENGINE.md).

Issue 1 — root cause: when Streamlit's source watcher fires, the next rerun in an open tab re-imports every project module. The `/auth/session` cookie route (bound once in `app.py`) kept the old in-memory ticket store while the login form wrote into the new one, so every login silently fell back to the login form, for all users, until restart. Reproduced on a scratch server.
- ✅ D-11.1 Streamlit source watching is off (`server.fileWatcherType = "none"`). Restart the server after code changes. Process-wide state (engine, DB engine) can no longer be duplicated by a re-import.
- ✅ D-11.2 Login tickets live in the database (`login_tickets`: SHA-256 of the ticket, user, session version, 60 s expiry, single use enforced by the DELETE). Works across re-imports and across server processes.
- ✅ D-11.3 A failed hand-off is never silent: the route redirects to `/?login_failed=<reason>` and the login form shows a Persian error; a cookie that no longer opens a session shows a notice. Logins, refusals, logouts and hand-off failures are logged.
- ✅ D-11.4 SDK clients are held for the whole call and closed afterwards. The Gemini adapter lost its client to garbage collection and failed every call with "Cannot send a request, as the client has been closed".
- ✅ D-11.5 Logs go to the console and to `data/logs/app.log` (rotating, 2 MB × 5, local only, never in Supabase). Windows' harmless asyncio "connection reset" noise at page navigation is filtered out.
- ✅ D-11.6 (Req. 1) Profile tab **"ارائه‌دهندگان سامانه"** before "ارائه‌دهندگان من" (analysts, guests): read-only list of admin-shared providers, models and keys (label + last 4) the user may use, computed by the same access rule as the analysis page.
- ✅ D-11.7 (Req. 2) Admins see the creator's username in the request list and in the request header. The creator was already stored (`analysis_requests.user_id`); non-admins still see only their own requests.

Issue 2 — root cause: the key picker created each key × model limiter slot with a semaphore of size 1 before the configured concurrency was applied, so every key ran one call at a time. Engine redesigned:
- ✅ D-11.8 **TPM removed** (model and key). Token use can't be known before a call (≈95k–125k per report) and providers enforce it with 429, which the cooldown handles. Real tokens are still recorded per document and per key × model × day.
- ✅ D-11.9 Limits keep the current scheme: **RPM and RPD = model default, key override**; **concurrency on the key**; all counted **per key × model** and shared by all users. Limit changes apply to running work within seconds, without restart.
- ✅ D-11.10 One **scheduler** per process decides every dispatch under one lock. Worker threads only make real API calls: waiting for a key, for RPM, for a cooldown or for a retry is a queue state, never a sleep in a thread. Capacity (key slot, RPM call, RPD reservation, server slot) is reserved before dispatch and released when the call ends, so RPD can't be exceeded by parallel calls.
- ✅ D-11.11 **Server-wide cap** on simultaneous API calls, admin-editable in Settings, **default 20**, applies immediately. The per-request cap of 4 is removed. No per-user cap for now (future option).
- ✅ D-11.12 **Fairness**: when capacity is short, turns rotate by user, then by request in submission order.
- ✅ D-11.13 Status: "در حال تحلیل" only while a call runs; otherwise "در صف" with the exact reason (key busy, minute limit, cooldown, server busy, retry in N seconds). Retry rules of D-10.3 unchanged (delays become queue delays). Kept PDFs are copied to storage when each document finishes, not all before the first analysis.
- ✅ D-11.14 Admin **"وضعیت موتور"** panel in Settings: per key × model running/max, calls in the last minute vs RPM, today vs RPD, cooldown; queue per running request; server running/cap. Dispatches, finishes, cooldowns and daily-limit exhaustion are logged.
- ✅ D-11.15 "Test connection" stays outside the limits (unchanged).
- ✅ D-11.16 (owner test, R-20261009-WXY5) A model item with an unexpected shape (e.g. a checklist item nested under an extra key, or a missing field) must never fail a document: every sheet row is stored with all its columns (missing = NULL), anything else stays in the row's `extra` and in the raw JSON, and a warning is logged. If writing a result to the database fails for any reason, the document still succeeds and its result is kept in memory like a guest's (Excel export), with a reason; usage is still counted. Structure is not repaired (D-7.1 unchanged).
- ✅ D-11.17 (owner rule) **Nothing from a result may be lost; no length limit on variable text.** Model output is stored in full: sheet values `Text`, unexpected keys `JSON` (`extra`), the complete answer as gzip bytes. File names, document reasons and a key's last error are `Text` too (migration 0005). Status columns show a short provider error; the full error text of every failed attempt is written to the log. A guard test fails if any such column gets a length limit. ("… characters truncated" in an error message is SQLAlchemy shortening values for display, not stored data.)
- ✅ D-11.18 (owner test: "Extra data: line 1 column 21910") **Answers that aren't a clean JSON.**
  - A complete JSON followed by extra text is **accepted with a warning**. The document shows "موفق – با هشدار" with the reason; the warning is stored on the document and the result (`warnings`, code `extra_text_after_json`); the full answer is kept in `raw_outputs` (kind `accepted`).
  - Any other unusable answer (invalid JSON, wrong structure, cut off at the output limit, refusal) is still rejected and retried as before, but **every such answer is kept verbatim** in `raw_outputs` (kind `rejected`, one row per attempt) and the document is flagged (`rejected_answer_kept`).
  - Kept answers count toward the user's results quota. Guests keep them in memory only; if the quota is full they are also kept in memory.
  - Their tokens are recorded, and they count toward the key's RPD, since they were paid. They don't use document quota.
  - The analysis page offers each kept answer as a text download.

## 12. Step 2b — upload, history, sharing, Excel, usage (decided 2026-10-09)
Owner accepted every recommendation of the walkthrough (Q1–Q14).
- ✅ D-12.1 (Q1) **Version picker.** The plan table stays as the overview. Every uploaded file that has earlier results gets its own dropdown: "تحلیل جدید" plus every version (Jalali date · provider › model, ⚠ if the result had warnings; admins also see the owner's username). Same model+provider versions are listed first, then the others, newest first. Default = newest same model+provider version, otherwise new analysis (D-5.6). Two buttons set all dropdowns ("all: latest result of this model" / "all: new analysis"). Versions from **other models may be reused**: the request then mixes models, and the document table and the Excel status sheet show the model of every document.
- ✅ D-12.2 (Q2) The **History** page holds every past request; the analysis page shows only the request just started plus a link to History. Both pages use one shared request view (status, Excel, retry, re-upload, share, delete).
- ✅ D-12.3 (Q3) History filters: one search box (request id, file name, company name — compared with Level-B keys, D-7.4), date range (presets or a custom Jalali range), request status (incl. "has failed documents"), model; admins also filter by user.
- ✅ D-12.4 (Q4) History tab **"فایل‌های نگه‌داری‌شده"**: the user's kept PDFs (name, size, date, number of requests using it, total vs. quota); multi-select delete with confirmation; admins see everyone's. A PDF is physically removed when no user keeps it any more; results stay.
- ✅ D-12.5 Deleting a request (D-10.9): removes its document rows, its own results and kept raw answers and its shares; a result still reused by another request is kept until that request is deleted too. Running requests can't be deleted. Usage events stay (quota already used is not given back).
- ✅ D-12.6 (Q5) Requests are shared **with viewers only**, by the owner or an admin (table `request_shares`, migration 0007). Only active viewer accounts are offered; sharing can be removed any time.
- ✅ D-12.7 (Q6) A viewer sees a shared request's status cards, document table, Excel download and the owner's name — not the raw JSON, kept raw answers, PDFs or key labels.
- ✅ D-12.8 (Q7) Guests have no History menu item (already the case since Step 1). The guest's last request on the analysis page gains the Excel download and "available until HH:MM (Tehran time)".
- ✅ D-12.9 (Q8) Excel status sheet **«وضعیت_اسناد»** (first sheet): ردیف, شناسه_درخواست, شناسه_سند, نام_فایل, نام_شرکت, سال, وضعیت, توضیح (reason + all warnings), ارائه‌دهنده, مدل, منبع_نتیجه (request the result came from), زمان_تحلیل (Jalali, Tehran), تعداد_تلاش, توکن_ورودی, توکن_خروجی, دما_اعمال_شد, ذخیره_در_پایگاه, PDF_نگه‌داری_شد, SHA-256 (full hash only here). File name `<request_id>.xlsx`. Contains successful and reused documents and results kept only in memory while they are still there; failed documents only in the status sheet.
- ✅ D-12.10 (Q9) Each of the six v1 sheets starts with شناسه_درخواست, شناسه_سند, **نام_شرکت, نام_فایل**, followed by all v1 columns in v1 order (unchanged).
- ✅ D-12.11 (Q10) Styling now: sheets right-to-left, v1 look (header colour, borders, alternating rows, widths, wrapping), frozen header row, header filters, font Vazirmatn (Excel substitutes where it isn't installed). Step 4 only polishes.
- ✅ D-12.12 (Q11) A value longer than Excel's 32,767-character cell limit is cut **in Excel only** and ends with «… متن کامل در JSON»; the status sheet names the cell. Characters Excel can't store (control characters) are dropped in Excel only. Stored data is never changed.
- ✅ D-12.13 (Q12) Fields the model returned in unexpected places (`extra`, D-11.16) go to a last column **«سایر_فیلدها»** as JSON text, only in sheets where some row has them.
- ✅ D-12.14 (Q13) Usage bars in **My profile** (analysts: documents in the last 60 min / 24 h, results MB, files MB; guests: documents used of the lifetime cap and days left); orange from 70 %, red when full. The analysis page keeps its cards. User management's list gets "اسناد ۲۴ ساعت" and "مصرف (مگابایت)" columns. Full overview vs. Supabase limits stays in Step 4.
- ✅ D-12.15 (Q14) When a document to retry has no file left (no processing copy, no kept PDF), the request view lists the missing files and offers an upload box; uploaded PDFs are matched by SHA-256 (other files are rejected by name), then "retry" works as usual in the same request.
- ✅ D-12.16 Small fixes: the file uploader's English texts are replaced with Persian (CSS); file names are shown as isolated text (U+2068…U+2069) so "1398.pdf" is no longer shown as "pdf.1398"; deleting requests also deletes their kept raw answers.

## 13. Step 3 — results dashboard (decided 2026-10-09)
Owner: build with the recommended answers (walkthrough https://claude.ai/artifact/3LgHdGcr7W8vCs43JX3NKy, Q1–Q9); refinements follow the owner's test (D-0.5).
- ✅ D-13.1 (Q1) Source: "all my results" (admins: everyone's; viewers: requests shared with them) or one request. The request view gets an "open in dashboard" button that preselects its request.
- ✅ D-13.2 (Q2) The same PDF with several stored results counts once: the newest result within the source. A single request uses its own choice (version picker).
- ✅ D-13.3 (Q3) One document per company and year: by default the most recently analysed; a notice lists years with more than one document and the user can switch (per browser session). Nothing is summed twice. Documents without a recognisable year are listed but left out of the year charts.
- ✅ D-13.4 (Q4) Company key = Level-B key without the legal-form words «شرکت», «سهامی عام», «سهامی خاص», «با مسئولیت محدود». Displayed names stay as the model wrote them.
- ✅ D-13.5 (Q5) Manual company merges are saved per user (table `company_merges`, migration 0008), apply wherever that user sees the company, and can be undone. Guests don't merge.
- ✅ D-13.6 (Q6) Page with three tabs: **نمای کلی** (cards, risk and opinion distribution, summary table across companies), **تحلیل یک شرکت** (cards, the 7 v1 charts, tables), **ادغام شرکت‌ها**.
- ✅ D-13.7 Charts keep v1's meaning (median trend lines with each document as a faint dot, stacked bars, sunbursts, heatmap with v1's four colours), Plotly, Vazirmatn, Persian digits on axes; each keeps v1's explanation in a collapsed box; the violations legend gets its correct title; empty sections say why.
- ✅ D-13.8 (Q7) Heatmap rows: every topic with a status other than «مصداق ندارد» in some year, in v1's priority order then schema order; the signature-page topic is left out; a toggle shows all topics.
- ✅ D-13.9 (Q8) Company tables with a year filter: year summary, emphasis risks, violations, checklist findings (status ≠ «مصداق ندارد»), basis-of-opinion items. Excel of a company across requests: later, if wanted.
- ✅ D-13.10 (Q9) Guests get the dashboard for their last (in-memory) request; no merges tab.
- ✅ D-13.11 Enum values (opinion, risk level, checklist status) are matched on Level-B keys (D-7.4); values outside the schema's lists are shown in tables but not placed on the ordinal charts.

## 14. Round 1 refinements (owner's 11 points, decided 2026-10-10)
Walkthrough: https://claude.ai/artifact/XqyH2W6eiUfNVNKWvYFGtH. Owner accepted every recommendation, with two notes (in D-14.2 and D-14.11).
- ✅ D-14.1 (pt 1) While a request runs, its view also offers Excel (finished documents; unfinished ones are rows of the status sheet), raw JSON of finished documents, "show in dashboard" and sharing. These controls sit outside the auto-refreshing part. Re-run and delete need the request to be finished or stopped.
- ✅ D-14.2 (pt 2, Q1) «توقف درخواست» (owner, admins): documents not yet sent to the model become **«متوقف‌شده توسط کاربر»** (resumable, quota reservation released); calls already running finish and are kept. The request shows «در حال توقف» until they return, then «متوقف شده (توسط کاربر)». **Owner's note:** a document whose call finishes and is saved during stopping keeps its normal «موفق» status; it is never labelled stopped or interrupted.
- ✅ D-14.3 (pt 3, Q2) «اجرای مجدد» panel: the request's documents with checkboxes, filtered by status pills; duplicates are not selectable. Unfinished documents (failed, stopped, interrupted, skipped) continue in the same request; documents that already have a result (successful, reused) are analysed in a **new request linked to this one** ("re-run of R-…", `analysis_requests.parent_request_id`). Both results are kept.
- ✅ D-14.4 (pt 3, Q3) The panel preselects the request's model and keys; choosing another model sends all selected documents into a new linked request (one model per request, D-5.7). Files no longer available must be re-uploaded first (D-12.15).
- ✅ D-14.5 (pt 4, Q4) Per document: duration of the last model call and the total of all calls (sending the PDF → answer or error; queue waits not counted), accumulated over retries and re-runs in the same request. Shown in the document table and the Excel status sheet; Excel also gets the document's start and end time.
- ✅ D-14.6 (pt 5) Per document: input/output tokens of the last attempt and totals over all attempts that got an answer (successes and rejected answers). Attempts without an answer report no tokens (counted as 0, stated in the status sheet). A reused result shows 0 in the reusing request.
- ✅ D-14.7 (pt 6, Q5) Requests can be shared with **analysts and viewers** (replaces D-12.6's "viewers only"). A shared analyst sees status, documents, Excel, dashboard and raw JSON; no re-run, delete, re-share or PDF download; shared results are not offered as their own earlier versions. Analysts get a History tab «اشتراک‌گذاشته با من».
- ✅ D-14.8 (pt 6, Q6) Dashboard sources: "all my results", "all requests shared with me" (separate), or one request (own or shared).
- ✅ D-14.9 (pt 7) History is a list: per request a checkbox (owners/admins only) and a collapsible section whose title is the request summary; the full request view loads only when opened. 20 requests per page; «حذف انتخاب‌شده‌ها» asks once, skips running requests and names them.
- ✅ D-14.10 (pt 8, Q7) Input PDFs can be downloaded per document in the request view and from «فایل‌های نگه‌داری‌شده» (one PDF, several as a ZIP), by the owner and admins only. Available while kept or still in the processing folder.
- ✅ D-14.11 (pts 9–10, Q8) Risk and violation charts: one fixed colour per main category (schema order) in both the bar chart and the sunburst (outer ring: lighter shade of its category); charts in one row and both explanations in an aligned row below. Outer-ring labels show the **full text wrapped onto several lines**; Plotly hides labels too small for their slice and shows them once the slice is zoomed in (click). **Owner's note:** a fixed 25-character cut would stay cut after zooming, so it is not used.
- ✅ D-14.12 (pt 11) Providers, models and keys: one list per level, «افزودن …» always first, every item collapsed by default with an informative title (type, active, counts, last key error), content loaded when opened; the item being edited stays open after saving. Same component in System settings and in Profile › «ارائه‌دهندگان من».

## 15. Step 4 — deploy first (decided 2026-10-11)
Context: the owner accepted Steps 1–3 and round 1 as built (detailed checks later, in production) and wants the app on **Posit Connect Cloud (free plan)** as soon as possible. Decision page: https://claude.ai/artifact/WqVqBRkG1XZUK2HJVGcYy5. Owner accepted every recommendation.
- ✅ D-15.1 (Q1) Step 4 is reduced to deploy-readiness: remove v1 (`legacy/`, `config.yaml`, v1 fonts/CSS, v1-only packages; D-0.2), verify on Postgres, a quick escaping/ZIP-limit check, safe production settings, deploy guide (SETUP.md §7). **Deferred until after deployment:** Word/PDF report, Excel RTL polish, admin-editable defaults and the usage overview vs. Supabase limits, and the review-round items in PLAN.md.
- ✅ D-15.2 (Q2) Posit deploys from `main` of the existing public repo `abbasi2021/test_streamlit` (free plan = public repos only). `refactor/v2` is merged into `main`; the owner pushes. This replaces the live v1 content. v1's `config.yaml` stays in git history (public since January): its passwords count as exposed.
  **Changed by D-15.7 (owner, 2026-10-11):** deploy from the owner's own repo instead.
- ✅ D-15.3 (Q3) Two Supabase projects (D-3.1): the dev project's values go into the local `.env` for the Postgres/Storage check; the prod values only into Posit's content Variables.
- ✅ D-15.4 (Q4) While documents are running, the request view says to keep the page open: Posit stops an idle app (no open tab); unfinished documents are then marked «متوقف شده» at the next start and continue with «ادامه یا اجرای مجدد». Status reasons now name that button (was «تلاش مجدد»).
- ✅ D-15.5 Login without the cookie routes: before going to `auth/session` / `auth/logout`, the browser asks `auth/ping`. If the host does not serve the `st.App` routes (Streamlit then answers with its own page), the browser returns to the app root with the ticket instead, and the app redeems it itself: the user is logged in for that browser session only (a refresh asks for the password again). Safety net in case Posit does not launch `app.py` as an `st.App`.
- ✅ D-15.7 (owner, replaces the repo part of D-15.2) The code goes to the owner's own public repo `roozbahani-workspace/financial-doc-analysor` as **one clean snapshot commit** (no v1 code or `config.yaml` in its history). Local `main` = that snapshot and continues from it (remote `github`); the full history stays only in the local branch `refactor/v2` and the old repo `abbasi2021/test_streamlit` (remote `origin`, no longer pushed). Posit needs a **new** content item from the new repo; the old v1 content stays linked to the old repo until the owner deletes it.
- ✅ D-15.8 (found at the first deploy, 2026-10-11) Posit Connect Cloud starts Streamlit without `streamlit run`'s `st.App` discovery and executes `app.py` as the page script, which showed a white page. `app.py` now runs `streamlit_app.py` itself when it is executed as a page script (a script-run context exists) and builds the `st.App` only when imported at server start. On Posit the D-15.5 fallback applies: login per browser tab, no persistence across refresh. ❓ Open: persistent login on Posit would need a cookie set by browser script (not HttpOnly, unlike D-4.9) — to decide with the owner.
- ✅ D-15.6 `requirements.txt` = runtime only, Streamlit pinned to the tested minor (1.64.*, `st.App` is experimental) and no new major versions of other packages; `requirements-dev.txt` adds pytest and playwright. The prompt guard keeps the v1-verified fingerprint inside the test (D-2.2). `TEST_DATABASE_URL` runs the database tests and a migration check against Postgres.
