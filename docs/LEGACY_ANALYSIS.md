# v1 (`app.py`, later `legacy/app_v1.py`) — How it works & known problems

Line numbers refer to v1 `app.py` at commit `9f295f5` + owner's baseline change.

## How v1 works
1. `streamlit-authenticator` login (users in `config.yaml`, passwords hard-coded and re-hashed every rerun).
2. Sidebar: API keys (from `.env` `GOOGLE_API_KEYS` / `API_KEY_n`, or custom textarea), model select, "API limits" (tokens/min, requests/min, requests/day).
3. Tab 1: upload PDFs or ZIP → `process_files_concurrent_smart` → `ThreadPoolExecutor` → `FinancialAnalyzer.extract_table_from_page` → `genai.Client(base_url="https://api.avalai.ir")` with PDF bytes inline, prompt, system instruction, `response_schema`, temperature 0.5.
4. Results kept only in `st.session_state.results` as `[(filename, dict | {"error": ...})]`.
5. Tab 2: per-file cards + Excel download (one workbook per file + ZIP). Tab 3: stats cards/table. Tab 4: company selection/manual merge + 7 charts (matplotlib + Plotly).

Frozen prompt/schema/config: `FinancialAnalyzer.__init__` (schema, ~L1340–1714) and `extract_table_from_page` (prompt + config, ~L1718–1744).

## Problems found
### Security
- Plain-text passwords in code (L126); bcrypt re-hashing on every rerun; weak committed cookie key (`config.yaml`).
- Model output and filenames injected into `unsafe_allow_html` markdown (L2296, L2306, L2466, L3218…) → HTML injection.
- ZIP upload without limits (zip bomb), basename collisions overwrite files.

### Correctness
- Year extracted with `\d{4}` → Persian digits (`۱۳۹۸`) give `Unknown`/`None`; documents silently dropped from charts.
- Nested retries: 5 internal attempts × up to 3 outer rounds ≈ 15 calls per file; non-retryable errors (404, bad model) retried 5×.
- `current_api_key` may be undefined in `except` if client creation fails.
- `APIKeyManager` recreated every rerun (failure counters lost); crashes with empty key list (`ValueError`).
- Rate limits (RPM/RPD/TPM) only used to estimate worker count; never enforced; daily usage not tracked.
- Wrong failed count after retries (L702 decrements a counter never incremented for retried files).
- Violations chart legend titled "دسته اصلی ریسک" instead of violation category (L1249).
- `normalize_company_name` defined twice (L2476, L3115); second silently overrides.
- `plot_checklist_heatmap` mutates caller's DataFrame; heatmap silently omits lower-priority topics.
- `convert_to_excel` is `@st.cache_data` but returns temp-file paths in never-cleaned `mkdtemp()` dirs; called twice.
- Charts' `df.get('موضوعیت_دارد', True) == True` fragile.

### Architecture
- 3,658-line single file; all classes/functions defined inside `if authenticated:` → redefined every rerun.
- Processing runs inside the script run: refresh/navigation/timeout loses results; no persistence; same PDF re-analysed (and paid) each time.
- Dead code: `process_files`, `merge_excel_files`; unused deps: PyPDF2, requests, asyncio, etc.
- Hard-coded provider/base URL (AvalAI). Log showed 404s with a wrong `/v1` base URL.

### UI / design-rule violations
- Sidebar on the left; purple gradients instead of navy header + green title; emojis instead of icons.
- Three fonts mixed (B Mitra, Noto Naskh, Vazirmatn); matplotlib needs reshaper/bidi for Persian.
- Results and statistics split across tabs; order not cards → charts → tables.
- Excel not RTL; no Word/PDF export.
