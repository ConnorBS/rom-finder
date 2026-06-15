# app/ — Layout, Conventions & Background Tasks

## Directory Layout

```
app/
  main.py              # FastAPI app, lifespan, DEFAULT_SETTINGS, scheduler startup
  db/
    database.py        # SQLite engine (WAL/busy_timeout pragmas) + get_session dependency
    migrations.py      # Ordered, tracked schema migrations (run_migrations)
    models.py          # All SQLModel table classes
  routers/
    collection.py      # /collection — unified library+wanted view, bulk actions
    wanted.py          # /wanted — hunt list management, per-game source search
    goals.py           # /goals — per-game event objectives (master/beat/custom) + deadlines
    library.py         # /library — raw library browser
    downloads.py       # /downloads — download queue
    settings_router.py # /settings — all config
    scheduler.py       # /scheduler — scheduled task UI + run-now endpoints
    activity.py        # /activity — SSE tray data, /card-states JSON
    games.py           # /search — RA game search
    api.py             # /api — extension-facing JSON endpoints
    logs.py            # /logs — activity log viewer
  services/
    ra_client.py       # RetroAchievements API wrapper (RAClient + SYSTEMS dict)
    hasher.py          # Generic MD5 hasher (fallback)
    rahasher.py        # RA-specific hash algorithm (platform-aware)
    activity.py        # In-memory task tracker polled by sidebar tray
    autodiscover.py    # One-shot: check RA for new games in tracked systems
    scheduler.py       # Scheduled task runners + scheduler_loop()
    logger.py          # Structured app logging to app_logs table
    goals.py           # Goal auto-completion from the RA mirror (LOCAL) — evaluate_goals
    events.py          # Event import (one API call) + nightly auto-sync of growing RA events
    settings.py        # Canonical settings accessors + DEFAULT_SETTINGS (leaf: imports only app.db)
    library_roots.py   # Multiple ROM directories: LibraryRoot CRUD, per-dir console mapping,
                       #   the shared iter_rom_files walk, download_target, primary seeding/backfill
    title_utils.py     # Search query generation + result relevance (search == hunt)
    sources/           # ROM download sources
    cover_sources/     # Cover art sources
  templates/
    base.html          # Sidebar nav, activity tray, mobile layout
    collection.html    # Main collection page (cards + list view)
    wanted.html        # Wanted list page
    scheduler.html     # Scheduler page
    settings.html      # Settings page
    partials/          # HTMX swap targets
```

---

## Coding Conventions

### No new abstractions without a concrete need
Three similar functions is better than a premature base class. No error handling for impossible scenarios. No feature flags or backwards-compat shims.

### Comments only for non-obvious WHY
No docstrings on simple functions. Short inline comment only when the reason would surprise a reader.

### HTMX responses
Endpoints that are HTMX targets return `HTMLResponse` with a snippet. Use `hx-target` + `hx-swap="innerHTML"`. Feedback goes into `<div id="*-result">` or `<div id="*-feedback">` elements.

### Session usage
Always `with Session(engine) as session:`. Never hold a session open across an `await` — open a new one after each async gap.

**`Depends(get_session)` holds a pooled connection for the WHOLE request** — including any `await` inside it. So an async endpoint that does network I/O (source search, RA lookup, download) must NOT take `session = Depends(get_session)`. Instead read what it needs in a short `with Session(engine)` block, release it, then do the slow `await`s with no connection checked out (see `wanted_source_results`, `api_search`, `games.search/ra_search/ra_game_sources` — and `_fetch_cover` for the canonical read-then-network-then-write shape). The fix for a real prod outage: these search/hunt endpoints fired in parallel each held a connection while httpx waited ~20s on Cloudflare-blocked mirrors, draining the pool → `QueuePool limit ... connection timed out` 500s on every other page.

**Long batches must commit per-item, never one session across the whole loop.** A bulk loop (hash / rehash / verify) that opens one `with Session(engine)` and commits only at the end keeps SQLite's **write lock for the entire batch** — and because each `session.get()` in the loop autoflushes the prior pending write, the write transaction opens on the first iteration and stays open for minutes. Any concurrent write then exceeds `busy_timeout` (5s) and fails with `(sqlite3.OperationalError) database is locked` → an **uncaught 500** (e.g. the extension's `POST /api/wanted` Add-to-Wanted, which doesn't go through `applog`, so it only shows in uvicorn stdout — `docker logs`, not `/api/logs`). The canonical shape is `ra_verify.run_pass`: snapshot the work set in a short session, do the slow hash/lookup `await` with **no** session held, then open a **fresh** `with Session(engine)` per item and `commit()` immediately. The scheduler hash pass (`run_hash_check`), the scan pipeline's hash + RA-verify steps (`run_scan`), and the manual rehash (`collection._do_rehash`) were all converted to this (they previously held one batch-long write txn — the cause of the intermittent Add-to-Wanted 500). `chd_format.run_chd_check` already did it right.

### Avoid blocking the event loop
Synchronous file I/O (hashing) → `await loop.run_in_executor(None, fn, *args)`. Network calls use httpx async client.

### Settings access pattern
Use the canonical leaf module **`app.services.settings`** (imports only `app.db`, so no import cycle — that was the original reason the per-router `_get_setting` was duplicated):
```python
from app.services import settings as app_settings
app_settings.get(session, key, default="")          # str
app_settings.get_bool(session, key, default=False)  # "true" → True
app_settings.get_json(session, key, default)         # GUARDED — bad JSON returns default, never raises
app_settings.set(session, key, value)
app_settings.get_extension_config(session, ext_id)   # {name: value} from ext_{id}_* keys
```
`DEFAULT_SETTINGS` also lives there (seeded in `main.py` lifespan). Always read JSON settings (e.g. `folder_map`) via `get_json` — a corrupted value must never 500 a page. The remaining per-module `_get_setting`/`_gs`/`_get` helpers delegate to `app_settings.get`; migrate call sites to `app_settings` directly when you touch them.

---

## Background Tasks & Activity Tray

### Pattern
FastAPI `BackgroundTasks` runs after the HTTP response in the same event loop. CPU-bound work (hashing) must use `run_in_executor`.

### Activity Store API (`app/services/activity.py`)
- `start(task_id, label, task_type)` — individual task
- `start_batch(task_id, label, total, task_type, entry_ids=None)` — batch with progress
- `increment(task_id)` — advance batch counter (use when there are no per-card overlays)
- `complete_entry(task_id, entry_id)` — advance AND drop that entry's per-card overlay. Use this (not `increment`) in `rehash`/`verify` batches so a finished card stops showing its spinner while the batch continues. Otherwise every entry stays lit until the whole batch ends.
- `finish(task_id)` — mark done (auto-pruned after 5s)
- `get_card_states()` → `{states: {"lib-N": "cover|rehash|verify", ...}, batch_types: []}`

### Tray polling
`base.html` polls `/activity/tray` every 3s via HTMX. `collection.html` polls `/activity/card-states` every 2s via JS for per-card overlays. Separately, a `base.html` poller hits `/api/changes` every 4s and **morphs the page in place** (idiomorph) when its scope's fingerprint changes — see `templates/CLAUDE.md` → *Live in-place updates* and `routers/CLAUDE.md` → `/api/changes`.

### Task ID conventions
- `"cover-{wanted_id}"` — cover fetch for a WantedGame
- `"cover-lib-{library_id}"` — cover fetch for a LibraryEntry
- `"cover-batch"` — bulk cover fetch
- `"rehash-batch"` — bulk rehash from collection page
- `"verify-batch"` — bulk RA verify
- `"scan-hash-batch"` — hash step inside scheduler scan pipeline
- `"sched-hash-batch"` — hash step inside scheduler hash-check task
- `"autodiscover"` — RA autodiscover pass
- `"chd-check-batch"` — CHD format check/re-encode pass (`chd_format.run_chd_check`)

### Card overlay scope gotcha
`batch_types` broadcast was removed. Only entries in `ActivityTask.entry_ids` get overlays. Always pass `entry_ids` to `start_batch()` for rehash/verify operations.
