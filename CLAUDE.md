# CLAUDE.md

Persian (RTL) Streamlit app that analyses Iranian **bank audit reports** (PDF) with an LLM, returns a fixed JSON structure, and shows dashboards + Excel exports. A full refactor ("v2") is in progress.

## Start here
1. [docs/PLAN.md](docs/PLAN.md) — steps, current status, target structure, data model. **Read "▶ Where we are" first: it says what's done, uncommitted and next.**
2. [docs/DECISIONS.md](docs/DECISIONS.md) — every agreed decision (`D-x.y`). **Source of truth.**
   - [docs/ENGINE.md](docs/ENGINE.md) — how analysis requests are scheduled (limits, retries, statuses).
3. [docs/LEGACY_ANALYSIS.md](docs/LEGACY_ANALYSIS.md) — how v1 works and its known bugs.
4. [docs/SETUP.md](docs/SETUP.md) — env, Supabase, run.
5. `ui ux design rules.docx` — mandatory UI rules (summarised in DECISIONS §9).

## Working agreement with the owner (mandatory)
- **Before each step (or batch of owner points):** hold a decision session. First explain how it works today (visually when it isn't trivial), then list only the real open points with a recommendation each and wait for answers. Never implement on assumed answers. What worked well: an Artifact page (today / proposal / questions, recommendations preselected, a copy-answers box); the owner pastes back "Q1: A …", sometimes with notes that refine an option. Their notes override the option text.
- **After each step (D-0.5, since 2b):** build, run the tests, check the UI on a scratch server, commit, then tell the owner exactly what to test. Their points come back as the next batch. Final review round after Step 4.
- Record every new/changed decision in `docs/DECISIONS.md` (one section per batch) and update "▶ Where we are" in `docs/PLAN.md`.
- Branch `refactor/v2`; one commit per step or batch.
- UI conventions: pages are direct scripts in `app_pages/`; use `ui/components.py` (help_box, cards_row, page_title, usage_bar, file_label) and key prefixes `danger_`/`warn_` for colored buttons; escape user/LLM text with `md_escape` / `html.escape`. One request view (`ui/request_view.py`) serves Analysis and History. Long lists use expanders with `key=` + `on_change="rerun"` and render content only when `.open` (lazy). Data grids are LTR: reverse columns and keep the total width inside the page (fixed pixel widths).

## Hard rules
- **Never change** the LLM prompt, system instruction, JSON schema or generation config (temperature 0.5). They live verbatim in `core/prompt_spec.py` (copied from v1, which was removed in Step 4); `tests/test_prompt_spec.py` guards their fingerprint. Only the transport per API type may differ.
- No hard-coded LLM provider/model/key; they come from the DB (admin/user configuration).
- Don't store uncritical data in Supabase (no logs, Excel files or chart images). Respect per-user quotas.
- LLM values are stored as text, normalized with Level A only; Level B only for comparison keys (`core/text_normalize.py`, D-7).
- Batch processing: one document's failure must never affect others; always report per-document status.
- Never put unescaped model output or filenames into `unsafe_allow_html` markup.
- UI: fully RTL, sidebar on the right, Vazirmatn, Material icons (not emojis), navy header `#0f172a→#1e293b` with title `#4ed793`, high-contrast buttons, results ordered cards → charts → tables.
- All user-facing text in Persian; code identifiers, comments and logs in English.

## Environment & commands
- Windows, conda env `fin-doc` (Python 3.13, Streamlit 1.64). Interpreter: `~/miniconda3/envs/fin-doc/python.exe`.
- Run: `streamlit run app.py` (inside `fin-doc`). `app.py` is an `st.App` launcher adding the cookie routes; the UI script is `streamlit_app.py` (running it directly breaks login persistence).
- v1 was removed in Step 4 (D-15.1). The public repo starts from a single snapshot commit (D-15.7); the full step-by-step history, including v1, is only in the owner's local branch `refactor/v2` (e.g. `git show v1-last:legacy/app_v1.py`; local tag `v1-last` = last commit that still had v1) and the old repo.
- Git remotes: `github` = the deploy repo `roozbahani-workspace/financial-doc-analysor` (push `main` there; Posit redeploys); `origin` = the old repo, no longer pushed.
- Source watching is off (D-11.1): **restart the server after any code change.** Re-enabling it splits process-wide state (engine, DB engine).
- Logs: console + `data/logs/app.log` (rotating). Never put API keys or passwords in log messages.
- Tests: `python -m pytest tests` (inside `fin-doc`, ~1.5 min). With `TEST_DATABASE_URL=postgresql+psycopg://…` the database tests run against that **throwaway** Postgres (its `public` schema is dropped per test), e.g. `docker run -d --name fda-pg -e POSTGRES_PASSWORD=fdatest -e POSTGRES_DB=fda -p 55432:5432 postgres:17`; engine tests use the mock provider (no network). In `tests/test_jobs.py`, `_mock_url(env, q)` always prepends `delay=0`, so a later `delay=` in `q` is ignored: set the provider URL directly for slow calls. `tests/test_pages_smoke.py` runs every page per role; when a page loads content only in opened sections, open them via session state (`<key>_exp = True`).
- Schema changes: edit `core/models.py`, then autogenerate a revision against a scratch SQLite DB (see `core/migrate.py`, `migrations/`), review it, and keep `tests/test_migrations.py` green. Never edit applied revisions.
- Real provider check (costs one API call): `python scripts/check_provider.py samples/1398.pdf` with `TEST_*` in `.env`.
- Mock provider (development only): API type `mock`, base URL like `mock://?delay=2&fail_rate=0.2&fail_kind=rate_limit&fail_names=1397`.
- UI testing without touching the owner's data: run a second server with `DATABASE_URL`/`LOCAL_STORAGE_DIR`/`LOG_FILE` pointing to a scratch folder and `--server.port 8511`; delete any `data/spool/<request>` folders it creates. `scripts/seed_scratch.py` fills the scratch DB (ali/Analyst123, viw/Viewer1234, admin/Admin1234, mock provider, sample requests); `scripts/ui_screenshots.py` takes Chrome screenshots (Playwright, `channel="chrome"`). Restart the scratch server after code changes (no source watching); CSS in `ui/styles.css` is re-read on every run.
- Config: `.env` (template `.env.example`). Empty `DATABASE_URL` / `SUPABASE_URL` → local SQLite + local folder storage.
- Deployment: Posit Connect Cloud (free plan) from `main` of the public repo; secrets are content Variables; see `docs/SETUP.md` §7. `requirements.txt` is runtime-only (Posit installs it); dev tools are in `requirements-dev.txt`.
