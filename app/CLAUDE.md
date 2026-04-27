# app/ — Layout, Conventions & Background Tasks

## Directory Layout

```
app/
  main.py              # FastAPI app, lifespan, _MIGRATIONS, DEFAULT_SETTINGS, scheduler startup
  db/
    database.py        # SQLite engine + get_session dependency
    models.py          # All SQLModel table classes
  routers/
    collection.py      # /collection — unified library+wanted view, bulk actions
    wanted.py          # /wanted — hunt list management, per-game source search
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
    title_utils.py     # Search query generation helpers
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

### Avoid blocking the event loop
Synchronous file I/O (hashing) → `await loop.run_in_executor(None, fn, *args)`. Network calls use httpx async client.

### Settings access pattern
```python
def _get_setting(session, key, default=""):
    s = session.get(AppSetting, key)
    return s.value if s else default
```
Duplicated across routers intentionally — do not consolidate (circular deps). Keep it local to each router/service.

---

## Background Tasks & Activity Tray

### Pattern
FastAPI `BackgroundTasks` runs after the HTTP response in the same event loop. CPU-bound work (hashing) must use `run_in_executor`.

### Activity Store API (`app/services/activity.py`)
- `start(task_id, label, task_type)` — individual task
- `start_batch(task_id, label, total, task_type, entry_ids=None)` — batch with progress
- `increment(task_id)` — advance batch counter
- `finish(task_id)` — mark done (auto-pruned after 5s)
- `get_card_states()` → `{states: {"lib-N": "cover|rehash|verify", ...}, batch_types: []}`

### Tray polling
`base.html` polls `/activity/tray` every 3s via HTMX. `collection.html` polls `/activity/card-states` every 2s via JS for per-card overlays.

### Task ID conventions
- `"cover-{wanted_id}"` — cover fetch for a WantedGame
- `"cover-lib-{library_id}"` — cover fetch for a LibraryEntry
- `"cover-batch"` — bulk cover fetch
- `"rehash-batch"` — bulk rehash from collection page
- `"verify-batch"` — bulk RA verify
- `"scan-hash-batch"` — hash step inside scheduler scan pipeline
- `"sched-hash-batch"` — hash step inside scheduler hash-check task
- `"autodiscover"` — RA autodiscover pass

### Card overlay scope gotcha
`batch_types` broadcast was removed. Only entries in `ActivityTask.entry_ids` get overlays. Always pass `entry_ids` to `start_batch()` for rehash/verify operations.
