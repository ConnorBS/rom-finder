# ROM Finder — Project Guide for Claude

## What This Project Is

A self-hosted web app for managing a personal ROM collection with a focus on **RetroAchievements (RA) hash verification**. The core workflow:

1. **Discover** — search RA's game database for games you want; add them to the Wanted pool
2. **Find** — search ROM sources (Archive.org, Vimm, etc.) and download ROMs
3. **Verify** — hash each ROM and match it against RA's accepted hash list
4. **Track** — maintain a library of verified ROMs, see cover art, know exactly which dump you have

The user wants every ROM they keep to be a hash-verified, RA-accepted dump — not just any ROM that runs.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, FastAPI (async) |
| Frontend | Jinja2 templates, HTMX 2, Tailwind CSS (CDN) |
| Database | SQLite via SQLModel (sync sessions, async routes) |
| Deploy | Docker on Unraid, GitHub Actions CI/CD → webhook triggers pull+restart |
| HTTP client | httpx (async) |

**No JS framework.** All interactivity is HTMX. JavaScript in templates is minimal — only for things HTMX can't do (dropdowns, card overlay polling, drag-to-reorder).

---

## Deployment

- Source: `https://github.com/ConnorBS/rom-finder`
- Docker image built by GitHub Actions on push to `main`
- Unraid server at `192.168.0.100`; app runs on port `19846`
- Appdata (DB + covers) lives at `/mnt/ssd_cache/appdata/rom-finder/`
- After pushing to `main`, GitHub Actions builds and a webhook triggers `docker pull` + container restart
- To verify a deploy landed: check the `rom-finder-webhook` container logs or inspect the image's revision label

---

## Directory Layout

```
app/
  main.py              # FastAPI app, lifespan, migrations, DEFAULT_SETTINGS, scheduler startup
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
    ra_client.py       # RetroAchievements API wrapper (RAClient class + SYSTEMS dict)
    hasher.py          # Generic MD5 hasher (fallback)
    rahasher.py        # RA-specific hash algorithm (platform-aware)
    activity.py        # In-memory task tracker polled by sidebar tray
    autodiscover.py    # One-shot: check RA for new games in tracked systems
    scheduler.py       # Scheduled task runners + scheduler_loop()
    logger.py          # Structured app logging to app_logs table
    sources/           # ROM download sources (archive.py, vimm.py, etc.)
    cover_sources/     # Cover art sources (retroachievements.py, steamgriddb.py)
    title_utils.py     # Search query generation helpers
  templates/
    base.html          # Sidebar nav, activity tray, mobile layout
    collection.html    # Main collection page (cards + list view)
    wanted.html        # Wanted list page
    scheduler.html     # Scheduler page
    settings.html      # Settings page
    partials/          # HTMX swap targets (activity_tray, search results, etc.)
```

---

## Data Models (`app/db/models.py`)

### `LibraryEntry` (table: `library`)
ROMs physically on disk.
```
id, game_title, system, file_name, file_path
file_hash        — MD5 or RA-hash string; None = not yet hashed
hash_verified    — True once looked up against RA
ra_game_id       — RA's numeric game ID, if known
ra_matched       — True if file_hash exists in RA's hash list
cover_path       — relative path under static/, e.g. "covers/1234.png"
hashed_at        — UTC datetime when hash was computed; used for stale detection
added_at
```

### `WantedGame` (table: `wanted_games`)
Games the user is hunting for, sourced from RA's catalog.
```
id, game_title, system
ra_game_id       — RA game ID (required; this is how RA is queried)
cover_path       — same format as LibraryEntry
status           — HuntStatus: hunting | verified
added_at, updated_at
```

### `Download` (table: `download`)
Active/completed download queue entries.
```
id, game_title, system, file_name, file_path
source_url, source_id, archive_identifier
status           — DownloadStatus enum (pending/downloading/hashing/completed/failed/verified/pending_approval)
progress         — 0.0–1.0
file_hash, hash_verified, ra_game_id
error_message, created_at, updated_at
```

### `AppSetting` (table: `app_settings`)
Simple key/value store. All settings live here; defaults seeded in `main.py::DEFAULT_SETTINGS`.

### `AppLog` (table: `app_logs`)
Structured log with `ts, level, category, message, details (JSON)`.

---

## Collection Status Vocabulary

The `/collection` page unifies `LibraryEntry` and `WantedGame` into one view. Each item gets a status:

| Status | Meaning |
|---|---|
| `verified` | WantedGame.status == verified — RA hash confirmed |
| `found` | Has a LibraryEntry linked by ra_game_id or title+system; wanted entry not yet verified |
| `library` | LibraryEntry only, no WantedGame |
| `wanted` | WantedGame only, no LibraryEntry yet |

---

## Settings Keys (important ones)

```
download_dir            — root ROMs directory (contains system subfolders)
check_dir               — staging area for ROMs awaiting approval
covers_dir              — where cover PNGs are saved (default: static/covers)
*_readonly              — locks: download_dir_readonly, check_dir_readonly, covers_dir_readonly
ra_username / ra_api_key
ra_enabled              — hash-verify downloads against RA after completion
folder_map              — JSON: {system_name: folder_name} overrides DEFAULT_FOLDER_MAP
cover_sources_order     — JSON array of source IDs in priority order
cover_source_{id}_enabled / cover_source_{id}_api_key
source_{id}_enabled     — ROM download sources
sched_scan_enabled / sched_scan_time / sched_scan_last_run
sched_hash_enabled / sched_hash_time / sched_hash_last_run
sched_autodiscover_enabled / sched_autodiscover_time / sched_autodiscover_last_run
```

---

## Schema Migrations

SQLite doesn't support dropping columns. New columns are added in `main.py::_MIGRATIONS`:

```python
_MIGRATIONS = [
    ("table_name", "column_name", "SQL_TYPE", "DEFAULT_EXPR or None"),
    ...
]
```

`None` default = nullable. Non-null defaults use a SQL expression string (e.g. `"''"`, `"0"`).
`_run_migrations()` runs at startup via `lifespan`. **Never use Alembic — keep using this pattern.**

---

## Background Tasks & Activity Tray

### Pattern
FastAPI `BackgroundTasks` runs after the HTTP response in the same event loop. CPU-bound work (hashing) must be offloaded: `await loop.run_in_executor(None, hash_rom, path, system)`.

### Activity Store (`app/services/activity.py`)
In-memory module-level dict of `ActivityTask` objects. Key functions:
- `start(task_id, label, task_type)` — individual task
- `start_batch(task_id, label, total, task_type, entry_ids=None)` — batch with progress
- `increment(task_id)` — advance batch counter
- `finish(task_id)` — mark done (auto-pruned after 5s)
- `get_card_states()` — returns `{states: {"lib-N": "cover|rehash|verify", ...}, batch_types: []}` for per-card overlays

### Tray polling
`base.html` polls `/activity/tray` every 3s via HTMX. `collection.html` polls `/activity/card-states` every 2s via JS to show per-card activity overlays.

### Task IDs (conventions)
- `"cover-{wanted_id}"` — cover fetch for a WantedGame
- `"cover-lib-{library_id}"` — cover fetch for a LibraryEntry
- `"cover-batch"` — bulk cover fetch
- `"rehash-batch"` — bulk rehash from collection page
- `"verify-batch"` — bulk RA verify
- `"scan-hash-batch"` — hash step inside scheduler scan pipeline
- `"sched-hash-batch"` — hash step inside scheduler hash-check task
- `"autodiscover"` — RA autodiscover pass

---

## RetroAchievements Integration

### API client (`app/services/ra_client.py`)
`RAClient(username, api_key)` — all methods are async.

Key methods:
- `get_game_list(system_id)` → list of games with `ID, Title, NumAchievements, DateModified`
- `get_game_hashes_full(game_id)` → list of `{MD5, Name, Labels}`
- `lookup_hash(md5)` → game info dict if matched, else None
- `get_game_info(game_id)` → full game detail
- `test_credentials()` → `(bool, message)`

### RA Hash Algorithm (`app/services/rahasher.py`)
RA doesn't always use plain MD5. Many systems use a platform-specific algorithm (e.g. NES strips the iNES header, N64 uses a specific byte range). `compute_ra_hash(path, system)` returns the correct hash for the platform, falling back to `None` if the system isn't handled. The caller then falls back to plain MD5 via `hash_rom()`.

### System IDs
`SYSTEMS` dict in `ra_client.py` maps RA's numeric console IDs to display names. This is the authoritative list. `DEFAULT_FOLDER_MAP` maps system names to typical folder names on disk.

### Cover filenames
`{ra_game_id}.png` when RA ID is known; `lib_{library_id}.png` for library entries with no RA ID.

---

## ROM Sources (`app/services/sources/`)

Each source extends `BaseSource` with:
- `source_id: str`
- `name: str`  
- `available: bool` — False = UI shows "coming soon", endpoint disabled
- `async search(query, system) → list[dict]`

Currently implemented: `archive_org` (Archive.org No-Intro/Redump search). `vimm`, `romsfun`, `wowroms` are stubs.

---

## Cover Sources (`app/services/cover_sources/`)

Each source extends `BaseCoverSource` with:
- `source_id: str`
- `name: str`
- `requires_api_key: bool`
- `async fetch_cover(ra_game_id, title, system, config) → bytes | None`

Sources tried in priority order (configurable via drag-to-reorder in Settings). First source returning bytes wins. Currently: `retroachievements` (uses RA game ID), `steamgriddb` (title-based search, requires API key).

---

## Scheduler (`app/services/scheduler.py`)

Three daily tasks, each configurable via `/scheduler`:

| Task | What it does |
|---|---|
| **Library scan** | Walk `download_dir` for new ROMs → insert → hash → fetch cover → RA verify |
| **Hash check** | Backfill `hashed_at` on existing entries; clear stale hashes (mtime > hashed_at); hash un-hashed entries |
| **RA autodiscover** | Call `get_game_list()` for each tracked system; add games with new achievement sets (DateModified > last_checked) to Wanted |

`scheduler_loop()` wakes every 60s, checks `_should_run()` for each enabled task against their configured local time. Started as an `asyncio.create_task()` in `lifespan`.

---

## Coding Conventions

### No new abstractions without a concrete need
Three similar functions is better than a premature base class. Don't add error handling for scenarios that can't happen. No feature flags or backwards-compat shims.

### Comments only for non-obvious WHY
Well-named code doesn't need comments. No docstrings on simple functions. A short inline comment is only added when the reason would surprise a reader.

### HTMX responses
Endpoints that are HTMX targets return `HTMLResponse` with a snippet. Use `hx-target` + `hx-swap="innerHTML"`. Feedback goes into dedicated `<div id="*-result">` or `<div id="*-feedback">` elements.

### Session usage
Always use `with Session(engine) as session:` blocks. Never store a session across await points — open a new one after each await if DB access is needed again.

### Avoid blocking the event loop
Synchronous file I/O (hashing) must use `await loop.run_in_executor(None, fn, *args)`. Network calls use httpx async client.

### Settings access pattern
```python
def _get_setting(session, key, default=""):
    s = session.get(AppSetting, key)
    return s.value if s else default
```
This pattern is duplicated across routers — do not consolidate into a shared import (would create circular deps). Keep it local to each router/service.

---

## Adding a New Feature — Checklist

1. **New DB column?** Add to `models.py` + add entry to `_MIGRATIONS` in `main.py`. Nullable columns use `None` default; non-null use a SQL string literal.
2. **New setting?** Add to `DEFAULT_SETTINGS` in `main.py`. New settings are seeded automatically at startup.
3. **New router?** Register in `main.py::app.include_router(...)`. Add nav link in `base.html` if user-facing.
4. **New background task?** Use `activity_store.start_batch(..., entry_ids=[...])` so card overlays target specific entries only, not the entire visible page.
5. **New cover source?** Subclass `BaseCoverSource`, place in `app/services/cover_sources/`, register in `registry.py`. Add enable/API-key settings to `DEFAULT_SETTINGS`.
6. **New ROM source?** Same pattern under `app/services/sources/`.

---

## Known Gotchas

- **`hashed_at` backfill**: Entries hashed before the `hashed_at` column was added have `file_hash != None, hashed_at = None`. The scheduler hash-check backfills these on first run.
- **RA cover source with no ID**: `ra_game_id = None` must be guarded before calling the RA cover API or it makes a request with `?i=None`. Guard: `if not ra_game_id: return None`.
- **Card overlay scope**: `batch_types` broadcast (marking ALL library cards) was removed. Only entries listed in `ActivityTask.entry_ids` get overlays. Always pass `entry_ids` to `start_batch()` for rehash/verify operations.
- **SQLite + async**: Sessions are sync. Never hold a session open across an `await`. Re-open after every async gap.
- **Docker path**: ROM file paths are stored as they appear inside the container. The DB and covers persist at `/mnt/ssd_cache/appdata/rom-finder/` via Docker volume.
- **`sched_autodiscover_*` vs `ra_autodiscover_*`**: The old `ra_autodiscover_*` settings still exist in the DB but are unused — the scheduler now uses `sched_autodiscover_*`. Don't remove the old keys; they're harmless.

---

## Future Work (user's stated intentions)

- **Hash export**: Generate a hash list file (CSV/JSON) of verified ROMs for sharing or importing into emulator frontends
- **Emulator integration**: Launch a game in a configured emulator directly from the collection view
- **More ROM sources**: Vimm, ROMsFun, WowROMs (stubs already exist)
- **RA progress tracking**: Show achievement completion % per game (requires RA user progress API)
- **Bulk RA verify after hash**: After hash-check runs, auto-verify any newly-hashed entries against RA
- **Import from existing collection**: Point at a folder of already-owned ROMs and bulk-import + hash + match in one pass
- **Notification on autodiscover**: Alert when new games are added to the Wanted pool by the scheduler
