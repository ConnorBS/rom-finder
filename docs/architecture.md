# ROM Finder — Architecture Decisions

Documents the *why* behind key technical choices. The *what* is in `CLAUDE.md`; this file records the reasoning so future decisions stay consistent.

---

## Stack choices

### FastAPI + HTMX (no SPA framework)
**Why:** The app is a single-user self-hosted tool. React/Vue/Svelte add build pipelines, bundle sizes, and JS state management overhead that a personal tool doesn't need. HTMX gives full interactivity (streaming progress, per-card overlays, partial swaps) with server-rendered HTML and minimal JS. Every interactive behaviour maps to one HTMX attribute + one FastAPI endpoint — easy to reason about and debug.

**Trade-off:** Client-side state (filter dropdowns, wanted list sorting) requires small inline JS. Accepted for cases where server round-trips would feel laggy.

### SQLite via SQLModel
**Why:** Single user, single server. SQLite is zero-config, backed up trivially (`cp rom_finder.db`), and fast enough for a few thousand rows. SQLModel gives Pydantic-style models with no separate ORM boilerplate.

**Trade-off:** Concurrent writes from background tasks (scheduler, multiple simultaneous downloads) can produce SQLite lock errors. Mitigated by opening short-lived `Session` blocks and never holding sessions across `await` points. Would need PostgreSQL for multi-user.

### Synchronous sessions inside async routes
**Why:** SQLModel's sync driver was chosen over the async SQLAlchemy driver because the async driver adds complexity (async context managers, requires `asyncio.run()` wrappers in background tasks) for no real benefit with SQLite. The rule is: **open a new session after every async gap** — this is strict and documented in every module.

### Separate `check_dir` / `download_dir` flow (optional)
**Why:** Gives the user a safety gate. Downloaded ROMs sit in a staging area where the user can verify hash status before the file lands in the organised collection. A read-only lock on the ROMs dir prevents the app from accidentally modifying an already-curated library.

**When to disable:** Users with trusted sources (Archive.org No-Intro sets) can set `use_review_dir=false` to skip the gate and auto-import directly.

---

## Data model decisions

### `WantedGame.ra_game_id` is non-nullable
**Why:** The RA game ID is the primary identifier used for hash lookup, cover art, and deduplication. A WantedGame without an RA ID has no path to verification. All games are added via the RA search endpoint so the ID is always known at add time.

### `LibraryEntry.cover_path` stores a relative path (`covers/1234.png`)
**Why:** The covers directory is configurable and may be outside the app container (e.g. `/mnt/ssd_cache/appdata/rom-finder/covers`). Storing a relative path under `static/` makes the path portable between container rebuilds. The `/static/` prefix is prepended at render time in templates.

### Separate `file_hash` + `hash_verified` + `ra_matched` booleans
**Why:** Three distinct states exist: (1) file hashed but not yet looked up, (2) looked up but not found in RA, (3) found in RA. A single boolean can't represent all three. `hash_verified` = "we ran the RA lookup"; `ra_matched` = "it was found".

### `hashed_at` timestamp on `LibraryEntry`
**Why:** The scheduler's hash-check task needs to know if a file has been modified since it was last hashed. Without `hashed_at`, the only option is to re-hash every file on every run, which is expensive for large collections.

---

## Background task design

### FastAPI `BackgroundTasks` (not Celery/RQ)
**Why:** Single-server, single-user. Celery adds Redis/RabbitMQ infrastructure overhead that is completely unjustified here. FastAPI's built-in background tasks run in the same event loop — simple and zero-dependency.

**Limitation:** If the server restarts mid-download, the task is lost. Accepted trade-off; the download record stays in the DB with `status=downloading` and the user can see it failed.

### In-memory activity store (`app/services/activity.py`)
**Why:** The activity tray needs live progress without WebSockets. HTMX polling every 2–3 seconds against a lightweight in-memory dict is cheaper to implement than a full SSE or WebSocket stream, and accurate enough for the use case.

**Trade-off:** Activity state is lost on restart. Fine — downloads are already restartable.

---

## Source / cover source registry pattern

### Plugin-style registry with `available: bool`
**Why:** Sources that aren't yet implemented (Vimm's Lair post-bot-protection, ROMsFun, WowROMs) should appear in the UI as "Coming soon" without breaking anything. The `available=False` flag disables the enable checkbox while keeping the source visible so users know future support is planned.

### Cover source priority order
**Why:** Different users have different preferences. RA covers are correct for RA-verified ROMs; SteamGridDB has better art for many games. Priority is configurable via drag-to-reorder in Settings — the order is stored as a JSON array in `cover_sources_order`.

---

## Router-per-page structure

### Why `_get_setting()` is duplicated across routers
**Reason:** A shared `settings` module would need to import the database engine, which is imported by the models, which are imported by the routers — circular import. The one-liner is not worth introducing an abstraction that breaks the import graph.

### Why `collection.py` is the heaviest router
`/collection` is the app's main page. It merges two tables (`library` + `wanted_games`) into a unified status vocabulary, handles bulk actions for hash/verify/cover/scan, and serves per-entry cover refresh endpoints. This complexity is justified — `/collection` is where users spend most of their time.

---

## Template partials strategy

Partials in `app/templates/partials/` are HTMX swap targets — they render one specific slice of the page in response to a user action. The rule: **if an endpoint returns HTML that replaces part of the page, that HTML belongs in a partial.**

Full-page templates in `app/templates/` inherit from `base.html` and are rendered by `TemplateResponse`. Partials are rendered the same way but are typically returned as `HTMLResponse` from specific HTMX endpoints.
