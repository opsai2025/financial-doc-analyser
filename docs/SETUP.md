# Setup Guide

## 1. Python environment
The project uses the conda env `fin-doc` (Python 3.13).
```bash
conda activate fin-doc
pip install -r requirements-dev.txt   # app + pytest + playwright
```
`requirements.txt` holds only what the app needs at runtime; it is what Posit Connect Cloud installs.

## 2. `.env`
```bash
cp .env.example .env
```
Fill in the values (see comments in the file). With `DATABASE_URL` and `SUPABASE_URL` left empty the app runs fully locally (SQLite + `data/storage`), which is fine for development.

## 3. Supabase (two free projects)
Create the two projects at https://supabase.com/dashboard:

| Project | Use | `.env` |
|---|---|---|
| `fin-doc-prod` | production | on the production server |
| `fin-doc-dev`  | development / testing | on your machine |

For each project:
1. **Database password** — set when creating the project; keep it safe.
2. **DATABASE_URL** — Dashboard → **Connect** → **Session pooler** → copy the URI, replace `[YOUR-PASSWORD]`, and change the prefix `postgresql://` to `postgresql+psycopg://`. (The *direct* connection is IPv6-only and often fails on Windows/home networks.)
3. **SUPABASE_URL** — Project Settings → API → Project URL.
4. **SUPABASE_SERVICE_KEY** — Project Settings → API keys → *secret* / `service_role` key. Server-side only.
5. Storage bucket — the app creates the private bucket `SUPABASE_STORAGE_BUCKET` on first run; nothing to do manually.

Tables are created automatically on first start.

### Free-tier limits to keep in mind
- 500 MB database, 1 GB storage per project — the app enforces per-user quotas and reports usage to admins.
- **Projects pause after ~7 days without activity.** Resume from the dashboard, or keep the app in regular use.

## 4. Run
```bash
streamlit run app.py
```
First login: `ADMIN_USERNAME` / `ADMIN_INITIAL_PASSWORD` from `.env`; you will be asked to set a new password. Then configure providers, models and API keys in the Admin panel.

## 5. Configure an LLM provider (in the app)
1. Log in as admin → **تنظیمات سامانه › ارائه‌دهندگان، مدل‌ها و کلیدها**.
2. Add a provider: name, API type and base URL. Examples:
   | Provider | API type | Base URL |
   |---|---|---|
   | AvalAI, Gemini models (native) | Google Gemini | `https://api.avalai.ir` |
   | AvalAI, OpenAI-compatible | سازگار با OpenAI | `https://api.avalai.ir/v1` |
   | Google directly | Google Gemini | *(empty)* |
   | OpenAI / Anthropic directly | OpenAI / Anthropic | *(empty)* |
3. Add models (API model id, e.g. `gemini-2.5-pro`), optional default limits (RPM/RPD) and switch off "ارسال دما" for models that reject temperature.
4. Add API keys (set "حداکثر درخواست هم‌زمان"; RPM/RPD only if the key differs from the model), then use **آزمون اتصال**.
5. **دسترسی‌ها**: give the analyst and/or guest role access (all or selected models/keys); add per-user overrides if needed.

Users can also add private providers in **پروفایل من › ارائه‌دهندگان من**; the providers shared with them are listed in **ارائه‌دهندگان سامانه**.

The server-wide cap on simultaneous API calls (default 20) is in **تنظیمات سامانه › مقادیر پیش‌فرض**; live key usage is in **وضعیت موتور**. How limits work: [ENGINE.md](ENGINE.md).

Logs: console and `data/logs/app.log` (on Posit Connect Cloud: the content's **Logs** panel).

## 6. Optional: verify a real provider from the command line
Fill `TEST_PROVIDER_TYPE` (`gemini`, `openai_compatible` or `anthropic`; other spellings such as `OpenAI-compatible` are accepted), `TEST_BASE_URL`, `TEST_API_KEY`, `TEST_MODEL` in `.env`, then:
```bash
python scripts/check_provider.py samples/1398.pdf
```
It prints duration, tokens and schema violations and saves the raw JSON to `data/`.

## 7. Deploy on Posit Connect Cloud (free plan)
The free plan publishes from a **public** GitHub repository: `roozbahani-workspace/financial-doc-analysor`, branch `main`.
Secrets are never in the repository: they are content **Variables** on Posit (read with `os.getenv`; Posit does not support `st.secrets`).

### 7.1 Before the first deploy
1. A Supabase **production** project (section 3) and its 3 values: `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
2. Two new secrets, generated once and stored safely (e.g. a password manager):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"                               # APP_SECRET_KEY
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # APP_ENCRYPTION_KEY
   ```
   Losing or changing `APP_ENCRYPTION_KEY` makes all stored API keys unreadable (re-enter them in the app).
   Changing `APP_SECRET_KEY` only logs everyone out.

### 7.2 Variables (content → Settings → Variables)
| Name | Value |
|---|---|
| `APP_ENV` | `production` (hides the mock provider) |
| `LOG_LEVEL` | `INFO` |
| `DATABASE_URL` | prod session-pooler URL, prefix `postgresql+psycopg://` |
| `SUPABASE_URL` | prod project URL |
| `SUPABASE_SERVICE_KEY` | prod secret / service_role key |
| `SUPABASE_STORAGE_BUCKET` | `audit-pdfs` |
| `APP_SECRET_KEY` | from 7.1 |
| `APP_ENCRYPTION_KEY` | from 7.1 |
| `ADMIN_USERNAME` | e.g. `admin` |
| `ADMIN_INITIAL_PASSWORD` | temporary; changed at first login |
| `SESSION_HOURS` | `8` |
| `GUEST_RESULT_TTL_MINUTES` | `120` |


### 7.3 Publish
- **First time**: Publish → GitHub → repository `roozbahani-workspace/financial-doc-analysor`, branch `main`, primary file **`app.py`** (not `streamlit_app.py`: `app.py` adds the login-cookie routes), Python **3.13** (3.12 also works). Add the variables in the publish dialog (or right after, then Republish). Turn on "Automatically publish on push".
- **Updates**: push `main` (`git push github main`); Posit redeploys.
- The old v1 content (linked to the old repo) is not affected; delete it in Posit when v2 runs.

Tables and the storage bucket are created automatically at the first start (migrations run on startup).

### 7.4 First checks after deploy
1. Open the app → log in with `ADMIN_USERNAME` / `ADMIN_INITIAL_PASSWORD` → set a new password.
2. **Refresh the browser**: you should stay logged in. If the refresh shows the login page again, Posit does not serve the cookie routes of `app.py`: the app then logs users in for the open browser tab only (the log says "Cookie routes are not served"). Everything else works; report it so it can be fixed.
3. Add a provider, model and key (section 5) → **آزمون اتصال** → analyse one PDF → download the Excel → open the dashboard.
4. Create the real users (Admin → User management).

### 7.5 Free-plan behaviour
- The app **sleeps** when no browser tab is open and wakes on the next visit (first load is slower). Work that was running when it stopped is marked «متوقف شده»; continue it with «ادامه یا اجرای مجدد». The analysis page reminds users to keep the page open.
- The local disk is temporary: logs and spooled uploads are lost on restart (by design; PDFs and results are in Supabase).
- Running hours per month are limited (see your plan page).
- Supabase free projects pause after ~7 days without activity (section 3).
