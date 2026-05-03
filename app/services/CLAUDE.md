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

**RAHasher binary source**: RAHasher is **not** a standalone repo. It's distributed via `RALibretro` releases:
`https://github.com/RetroAchievements/RALibretro/releases/latest` → `RAHasher-x64-Linux-{version}.zip`
The Dockerfile downloads it at build time. Without it, disc-based systems (Saturn, PS1/2, Dreamcast, Sega CD, etc.) hash as plain MD5 of the image file and will never match RA's database.
A startup `print()` in `main.py` logs RAHasher availability to Docker stdout on every boot.

### Rate limiting

`_RateLimiter` (module-level `_limiter`) enforces 4 req/sec (240/min) across all `RAClient` instances and methods. RA's documented ceiling is 500 req/min; 240 gives a comfortable buffer. On a 429 response, `lookup_hash` waits for the `Retry-After` header value (default 60s) and retries once. A second consecutive 429 raises so the caller skips the entry and continues.

At 4 req/sec, a full bulk verify of ~10 000 entries takes ~42 minutes as a background task.

### RA API gotchas

**`API_GetGameInfoByMD5` is deprecated/broken**: Returns HTTP 404 for ALL hashes including ones confirmed in RA's database via `API_GetGameHashes.php`. `lookup_hash` now uses `dorequest.php?r=gameid&u={user}&m={hash}` instead, which returns `{"Success": true, "GameID": N}` (N=0 = not found, N>0 = found). This was confirmed by cross-checking: `API_GetGameHashes.php` returned hash `3132056c8f17e4088b95e4264ca59575` for game 724, but `API_GetGameInfoByMD5.php` returned 404 for that same hash.

**`dorequest.php` response format**: `{"Success": true, "GameID": N}` — normalised to `{"ID": N, ...}` before returning to callers.

**Legacy `API_GetGameInfoByMD5` response formats** (kept for reference):
1. Flat with `"ID"` — legacy format: `{"ID": 1234, "Title": "...", ...}`
2. Flat with `"GameID"` — emulator-style: `{"Success": true, "GameID": 1234}`
3. Wrapped in `"GameData"` — newer API format: `{"GameData": {"ID": 1234, ...}}`

All are normalised to `payload["ID"]` before returning. Callers always use `match.get("ID")`.

**Null response**: When hash not found, API returns JSON `null`. `lookup_hash` guards with `isinstance(data, dict)`.

**HTTP 404**: Returned for "not found" — handled by returning `None`, not raising.

**HTTP 429**: Raises `RuntimeError` with rate-limit message so caller's warning log is actionable.

**Non-JSON response** (e.g., Cloudflare challenge page): `lookup_hash` catches the parse error, logs a warning via Python `logging` (visible in Docker logs), and returns `None`.

**RA hash not matching**: Usually means the dump isn't in RA's hash list (different No-Intro dump). Check `/logs` → category "hash" — exact hash is logged. If `lookup_hash` returns no game ID despite a valid-looking response, a `WARNING` is emitted to Docker stdout with the raw response body.

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
