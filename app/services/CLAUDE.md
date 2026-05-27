# app/services/ — RA Integration, Sources, Scheduler

## RetroAchievements Integration

### API client (`ra_client.py`)
`RAClient(username, api_key)` — all methods async.

Key methods:
- `get_game_list(system_id)` → list of `{ID, Title, NumAchievements, DateModified}`
- `get_game_hashes_full(game_id)` → list of `{MD5, Name, Labels}`
- `lookup_hash(md5)` → game info dict if matched, else `None`. **Wrong-game guard:** a hash can match a *different* RA game than intended (a Solaris ROM downloaded during a Kirby hunt hashes to Solaris). BOTH verify paths must check the matched id equals the expected `ra_game_id` before marking verified — `downloads.py` (approval) and `hunter.py` (`_match_is_correct_game`, auto-hunt). A mismatch is recorded as `bad_hash` and the hunt continues.
- `get_game_info(game_id)` → full game detail
- `test_credentials()` → `(bool, message)`

`SYSTEMS` dict maps RA numeric console IDs → display names (authoritative list).  
`DEFAULT_FOLDER_MAP` maps system names → typical folder names on disk.

`RA_UNSUPPORTED_SYSTEMS` (+ `is_ra_unsupported(system)`): curated set of platforms RA has **no** console/hashing support for (e.g. `Nintendo 3DS`, `Archipelago`). ROMs on these can never hash-match, so the collection shows them as "unsupported" (not `no_ra`) and **every verify path skips them** so RA is never called for an unverifiable platform. Curated, NOT derived from `SYSTEMS` — misnamed-but-supported folders (`tg16`, `mega-duck-slash-cougar-boy`) DO verify, so excluding everything absent from `SYSTEMS` would wrongly hide real matches. Extend the set as such platforms appear.

**System name normalization (`title_utils.canonical_system(name, system_id)`)**: the RA console id is authoritative — resolve `SYSTEMS[id]` when present. Otherwise collapse an exact-doubled scraped name (`"WiiWii"` → `"Wii"`). It deliberately does NOT use an endswith/abbrev heuristic ("Super Nintendo Entertainment System" ends with NES's full name). Used by `api.py` (add-wanted + search) so corruption is fixed at the source regardless of what the Chrome extension posts; migration `0010` fixed existing rows, so the old every-startup WiiWii `UPDATE` is gone from `main.py` lifespan.

### Cover filenames
`{ra_game_id}.png` when RA ID is known; `lib_{library_id}.png` for entries with no RA ID.

### RA hash algorithm (`rahasher.py`)
RA doesn't always use plain MD5 — many systems use platform-specific algorithms (NES strips iNES header, N64 uses specific byte range, etc.). `compute_ra_hash(path, system)` returns the correct hash or `None` if the system isn't handled. Caller falls back to `hash_rom()` (plain MD5).

**Canonical source for hash algorithms: rcheevos `rc_hash`.** Every RA-approved emulator (RetroArch cores, RALibretro, RAHasher) hashes via the rcheevos `rc_hash` library, so it is the source of truth — **derive each console's algorithm from it rather than reverse-engineering by trial.** When adding a new console: read the dispatcher in [`src/rhash/hash.c`](https://github.com/RetroAchievements/rcheevos/blob/develop/src/rhash/hash.c) (`case RC_CONSOLE_*` → `rc_hash_<console>`), then the implementation in `hash_rom.c` (cart/ROM) or `hash_disc.c` (disc). Port that exactly into `hasher.py` (`compute_ra_hash`/RAHasher already covers disc + many carts). For disc systems the RAHasher binary IS rc_hash, so prefer it.

**Arduboy is text, not binary** (`md5_arduboy` in `hasher.py`): `rc_hash_arduboy` → `rc_hash_text` emits each `.hex` line's content + a single `'\n'`, normalizing `\r\n`/`\r`/`\n` to `\n` and always appending a trailing newline. `md5_arduboy` is a faithful port of that loop. Hashing raw bytes made every CRLF dump (the "Erwin's Collection" repacks) miss while LF dumps matched — confirmed against RA's accepted hash for game 9286 ("Under the Tower"). `.hex` is in both `ROM_EXTENSIONS` sets so zipped/unzipped Arduboy files are scanned + extracted.

**RAHasher binary source**: RAHasher is **not** a standalone repo. It's distributed via `RALibretro` releases:
`https://github.com/RetroAchievements/RALibretro/releases/latest` → `RAHasher-x64-Linux-{version}.zip`
The Dockerfile downloads it at build time. Without it, disc-based systems (Saturn, PS1/2, Dreamcast, Sega CD, etc.) hash as plain MD5 of the image file and will never match RA's database.

**Availability is surfaced (Phase 6), not silent:** `rahasher_status()` powers `/api/status.rahasher`; `main.py` startup also writes an `applog` info/warning (HTTP-visible via `/logs`, not just Docker stdout); and `/settings` shows an amber banner when it's missing. **Disc guard:** all hashing goes through `ra_hash_or_fallback(path, system) -> (hash, used_rahasher)`, which logs a clear WARNING when a `DISC_SYSTEMS` ROM falls back to MD5 because RAHasher is absent (`disc_without_rahasher()`), so it's diagnosable instead of masquerading as "not in RA database".

### Rate limiting

`_RateLimiter` (module-level `_limiter`) enforces 2 req/sec (120/min) across all `RAClient` instances and methods. RA's documented ceiling is 500 req/min; 120 gives a comfortable buffer — 4 req/sec still triggered 429s in practice. On a 429 response, `lookup_hash` waits for the `Retry-After` header value (default 60s) and retries once. A second consecutive 429 raises so the caller skips the entry and continues.

At 2 req/sec, a full bulk verify of ~10 000 entries takes ~83 minutes as a background task.

On a persistent 429, `lookup_hash` now raises **`SourceRateLimitError`** (from `sources/errors.py`, carries `retry_after`) instead of `RuntimeError`, so the resilient verify can back off intelligently.

### Resilient re-verify (`ra_verify.py`)
`run_pass()` is the rate-limit-aware, resumable bulk verify (replaces the old in-memory loop that the 2026-05-03 429 cut off, stranding ~2789 entries). It:
- derives the work set from the DB each pass (`repository.library_pending_ra_check` — hashed, unmatched, and `ra_checked_at` null or older than `stale_days`), so a crash/restart just recomputes the remainder;
- opens a **fresh Session per entry** (never across the `lookup_hash` await);
- on `SourceRateLimitError`, persists an escalating pause in `ra_verify_paused_until` and stops — the next pass (scheduler tick or manual run) honours it even across restarts;
- stamps `ra_checked_at` so genuine misses leave the work set (passes terminate; the daily scheduler pass doesn't re-hammer them).

Wired as the scheduler **`verify`** task (`sched_verify_*`, default 05:00) and the `/scheduler` Run-now button. State surfaces in `/api/status.verify`. Settings: `ra_verify_in_progress`, `ra_verify_paused_until`, `ra_verify_last_run`, `ra_verify_batch_size`.

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

### Auto-hunt candidate matching (`hunter.py`)
Each source returns candidate files; the hunter scores them against **RA's accepted ROM names** (`_file_score`) — that's the authoritative "is this the right dump?" check. Key rules:
- A score of **0 = unrelated** (matches neither an RA ROM-name stem nor the game title's significant words). Always skipped — works even when RA hashes failed to load (no region freebie). Stops a loose collection match (e.g. an NDS romset for a Wii hunt) from being downloaded.
- `_significant_terms(title)` provides a title fallback when RA stems are unavailable.
- **Hard cap** `_MAX_CANDIDATES` (20) on files attempted per hunt — prevents the "hundreds of downloads" flood.
- Token-CDN sources (ROMsFun, WowROMs) can 403 from anti-leech protection even with a fresh signed token; these surface as `SourceForbiddenError` and the hunt moves on. **Archive.org (direct download, no token) is the reliable backbone.**

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
