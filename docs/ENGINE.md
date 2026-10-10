# Analysis engine: how requests are executed

Code: `services/jobs.py` (JobManager: planning, persistence, results), `services/scheduler.py`
(capacity and dispatch), `services/providers.py` (`runtime_keys`, `live_limits`),
`services/app_settings.py` (server cap). Decisions: DECISIONS §11 (D-11.8 – D-11.15).

Part 1 describes the engine as it is now. Part 2 records the Step 2a engine it replaced and why.

---

## Part 1 — Current engine

### 1.1 Lifecycle of a request

| # | Stage | Thread | What happens |
|---|---|---|---|
| 1 | Submit | page (`JobManager.submit`) | One document per file, checked in this order: duplicate in the batch, reuse of a previous result, too large, over the document quota. The rest are queued. PDFs are spooled to `data/spool/<request>`; non-guest requests are written to the DB. |
| 2 | Hand-over | page (`_start`) | Loads the model and decrypts the selected keys (`runtime_keys`). If none is usable, the queued documents fail at once. Otherwise `scheduler.add_run(...)`. |
| 3 | Planning | scheduler thread (`fda-scheduler`) | One pass at a time, under one lock: picks which waiting documents may start now and on which key, reserves the capacity, hands each start to a call thread. Waiting documents get a reason. Then sleeps until a call ends, a delay expires, or 10 s pass. |
| 4 | Call | call thread (`fda-call_N`) | Exactly one API call. Status "در حال تحلیل" is set here and nowhere else. No waiting or sleeping happens in this thread. |
| 5 | Outcome | call thread (it alone owns the document until it is queued again) | Capacity released. Success: result saved (or kept in memory), PDF copied to storage if kept. Retryable failure: the document goes back to the queue with a "not before" time. Otherwise: failed with a reason. |
| 6 | Finish | call thread of the last document | The request is completed; guest/unstored results move to the in-memory store. |

### 1.2 Capacity: what must be free before a call starts

Counted **per API key × model**, shared by every user and request (D-11.9). All must hold:

| Check | Setting | Where it's set | When it blocks |
|---|---|---|---|
| Active | key and model active | provider editor | Key skipped for the request ("inactive"). |
| Daily requests | **RPD** | model default, key override | Key skipped once used ≥ RPD. Calls in flight count as reserved, so parallel calls can't exceed RPD. Counter in the DB, per UTC day. |
| Cooldown | after a provider 429 | automatic: `Retry-After` or 60 s | Key waits until the cooldown ends. |
| Minute window | **RPM** | model default, key override | Starts in the last 60 s ≥ RPM → wait until the oldest start leaves the window. |
| Concurrent calls | **حداکثر درخواست هم‌زمان** | API key (1–10) | Running calls on this key × model ≥ the value → wait for one to end. |
| Server | **server-wide cap** | Settings › مقادیر پیش‌فرض (default **20**, 1–100) | Running calls on the server ≥ cap → wait. |

- Empty RPM/RPD means unlimited. There's no TPM (D-11.8); real tokens are still stored per document and per key × model × day.
- Limit changes are re-read every 10 s (immediately after saving the server cap) and apply to work already queued.
- Key choice: among the request's keys that can start now, the least loaded one; a retry avoids the key that just failed if another is free.

### 1.3 Turns between users (D-11.12)

When capacity is short, the user served least recently goes first (a user who just arrived goes first). Within a user, requests go in submission order; within a request, documents go in file order. So a second user's document starts as soon as one slot frees, even if the first user still has 40 documents queued.

### 1.4 Retries (D-10.3, unchanged rules)

| Error | Counts as an attempt? | Next |
|---|---|---|
| 429 rate limit | yes | key × model cooldown; retry after 10 s / 30 s (as a queue delay) |
| timeout, network, 5xx | yes | retry after 10 s / 30 s |
| invalid JSON / wrong structure | yes | answer kept (`raw_outputs`, rejected) and its tokens counted; retry after 10 s / 30 s |
| 401/403 invalid key | no | key excluded for this request, error saved on the key, retry at once on another key |
| other 4xx, too large, refusal, truncated output | yes | failed |

A complete JSON followed by extra text is not an error: it's accepted with a warning and the whole answer is kept (D-11.18).

Attempts per document = the model's "تعداد تلاش" (default 3). When no usable key is left, the waiting documents fail with the reason.

### 1.5 What the user sees

- "در صف" + reason while waiting:
  - "در انتظار ظرفیت کلید «X» (۵ از ۵ …)" — the key's concurrency is full
  - "سقف درخواست دقیقه‌ای … ادامه از حدود ساعت …" — RPM
  - "… پس از خطای ۴۲۹ موقتاً متوقف است …" — cooldown
  - "در انتظار ظرفیت سرور …" — server cap
  - "تلاش ۱ ناموفق بود؛ تلاش مجدد از حدود ساعت …" — a retry is scheduled
- "در حال تحلیل" only during a real call.
- Admin › تنظیمات سامانه › **وضعیت موتور** (refreshes every 3 s):
  - per key × model: running/max, starts in the last minute vs RPM, today vs RPD, cooldown;
  - per running request: user, running, waiting, waiting for retry, remaining.
- Log (`data/logs/app.log`):
  - INFO: `Queued`, `Call … attempt=N running=x/cap`, `Done … outcome= in Ns tokens=in/out`, `Request … finished`;
  - WARNING: `Cooldown`, `Daily limit reached`, `Key excluded`, `Attempt N failed …` (with the provider's full error text), `Unexpected item shape`.

### 1.5a Stop, re-run, time and tokens (D-14.2 – D-14.6)

- **Stop** (`JobManager.stop` → `Scheduler.cancel`): the run is marked cancelled; its waiting documents (incl. those waiting for a retry) leave the queue and become «متوقف‌شده توسط کاربر». Calls already running finish normally — a successful one is saved as «موفق» — and a failed one is not retried. The request is «در حال توقف» until the last call returns, then «متوقف شده (توسط کاربر)». Stopped documents are retryable.
- **Re-run** (`JobManager.rerun`): with the request's model, unfinished documents are re-queued in the same request (`retry` with a selection and keys); documents with a result go to a new request whose `parent_request_id` points back. Another model: everything selected goes to a new linked request.
- **Per attempt**, the scheduler reports `attempt_done(seconds, tokens)` to the JobManager: seconds = model time only (after the "processing" status is written, until the answer or error); tokens only when the provider answered. The document keeps the last attempt and totals (`last_call_seconds`, `total_call_seconds`, `total_input_tokens`, `total_output_tokens`), accumulated over retries and resumes in the same request.

### 1.6 Scope and limits of the design

- Running calls, RPM windows and cooldowns live in process memory; RPD is in the DB. Streamlit runs as one process; several server replicas would need a shared limiter (not planned).
- After a restart, unfinished documents become "متوقف شده" and can be retried (D-10.2).
- "Test connection" is outside the limits (D-11.15).
- Not implemented on purpose: per-user cap on simultaneous calls (D-11.11, possible later).

### 1.7 Measured

- One key, concurrency 5, 5 documents × 20 s mock calls: all 5 started within 0.1 s; the batch finished in 20 s.
- Tests (`tests/test_scheduler.py`) cover concurrency, server cap, sharing between users, turns, wait reasons, RPD not exceeded, live limit changes and the status snapshot.

---

## Part 2 — History: the Step 2a engine (replaced 2026-10-09)

### 2.1 How it worked

- A dispatcher thread per request put at most `min(Σ key concurrency, 4)` documents into a shared pool of 8 threads.
- Each pool thread:
  - marked its document "در حال تحلیل";
  - picked a key, read RPD from the DB, and blocked on a key × model semaphore;
  - slept in ≤ 5 s steps while cooldown, RPM or TPM said wait (20,000 tokens estimated per call);
  - made the call;
  - on a retryable error, slept 10 s / 30 s inside the thread.

### 2.2 Why your batch ran one document at a time (Issue 2)

The key picker called `wait_seconds()`, which created the key × model slot with a semaphore of size **1** before `acquire()` could create it with the configured concurrency. The slot was never resized, so every key behaved as concurrency 1.

- Measured: concurrency 5, 5 documents × 2 s → peak 1 call, 10.2 s.
- Request `R-20261008-WERZ` showed the same. Four documents showed "در حال تحلیل" from 20:42:00 Tehran time, but finished one by one: 20:45:17, 20:47:10, 20:50:46, 20:54:08, 20:56:22. Each call took 113–216 s.

### 2.3 Other defects found

| # | Defect |
|---|---|
| D2 | Key slots were never refreshed: limit changes needed a restart. |
| D3 | Documents showed "در حال تحلیل" while waiting for a key. |
| D4 | Waits and retry sleeps held the 8 shared threads, so one user's waiting work could delay everyone. |
| D5 | Hidden per-request cap of 4. |
| D6 | Concurrency counted per key × model *(kept by decision D-11.9)*. |
| D7 | TPM estimate 20k vs ≈110k real tokens; the correction could land on another call's entry. |
| D8 | RPD read-then-call: parallel calls could exceed it. |
| D9 | The key picker ignored whether a key was busy. |
| D10 | All PDFs were uploaded to storage before the first analysis. |

All are resolved by Part 1, except D6, which was kept by choice.
