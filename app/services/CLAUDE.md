# app/services/ — RA Integration, Sources, Scheduler

## RetroAchievements Integration

### API client (`ra_client.py`)
`RAClient(username, api_key)` — all methods async.

Key methods:
- `get_game_list(system_id)` → list of `{ID, Title, NumAchievements, DateModified}`
- `get_game_hashes_full(game_id)` → list of `{MD5, Name, Labels}`
- `lookup_hash(md5)` → game info dict if matched, else `None`
- `get_game_info(game_id)` → full game detail
- `test_credentials()` → `(bool, message)`

`SYSTEMS` dict maps RA numeric console IDs → display names (authoritative list).  
`DEFAULT_FOLDER_MAP` maps system names → typical folder names on disk.

### Cover filenames
`{ra_game_id}.png` when RA ID is known; `lib_{library_id}.png` for entries with no RA ID.

### RA hash algorithm (`rahasher.py`)
RA doesn't always use plain MD5 — many systems use platform-specific algorithms (NES strips iNES header, N64 uses specific byte range, etc.). `compute_ra_hash(path, system)` returns the correct hash or `None` if the system isn't handled. Caller falls back to `hash_rom()` (plain MD5).

### RA API gotchas

**`API_GetGameInfoByMD5` field names**: Legacy endpoint returns `"ID"`; newer docs say `"GameID"`. `lookup_hash` normalises both to `"ID"` — callers always use `match.get("ID")`.

**Null response**: When no hash found, API returns JSON `null` (not `{}`). `lookup_hash` guards with `isinstance(data, dict)` before `.get()`.

**HTTP 404**: Returned for "not found" — handled by returning `None`, not raising.

**HTTP 429**: Raises `RuntimeError` with rate-limit message so caller's warning log is actionable.

**RA hash not matching**: Usually means the dump isn't in RA's hash list (different No-Intro dump). Check `/logs` → category "hash" — exact hash is logged for manual lookup on retroachievements.org.

---

## ROM Sources (`sources/`)

Each source extends `BaseSource`:
- `source_id: str`
- `name: str`
- `available: bool` — False = UI shows "coming soon", endpoint disabled
- `async search(query, system) → list[dict]`

Implemented: `archive_org`. Stubs: `vimm`, `romsfun`, `wowroms`.

---

## Cover Sources (`cover_sources/`)

Each source extends `BaseCoverSource`:
- `source_id: str`
- `name: str`
- `requires_api_key: bool`
- `async fetch_cover(ra_game_id, title, system, config) → bytes | None`

Sources tried in priority order (drag-to-reorder in Settings). First returning bytes wins.  
Implemented: `retroachievements` (uses RA game ID), `steamgriddb` (title search, requires API key).

**`ra_game_id = None` guard**: The RA cover source must check `if not ra_game_id: return None` — otherwise makes a request with `?i=None`.

---

## Scheduler (`scheduler.py`)

Three daily tasks configured via `/scheduler`:

| Task | What it does |
|---|---|
| **Library scan** | Walk `download_dir` → insert new ROMs → hash → fetch cover → RA verify |
| **Hash check** | Backfill `hashed_at`; clear stale hashes (mtime > hashed_at); hash un-hashed entries |
| **RA autodiscover** | `get_game_list()` per tracked system; add games with new achievement sets to Wanted |

`scheduler_loop()` wakes every 60s, checks `_should_run()` per enabled task against configured local time. Started as `asyncio.create_task()` in `lifespan`.

**Run Now behaviour**: `POST /scheduler/run/{task_id}` is synchronous — HTTP response held until task completes (intentional for self-hosted, no proxy timeouts). UI shows spinner via `hx-indicator`; "Last run" timestamp updates via HTMX OOB swap after completion.
