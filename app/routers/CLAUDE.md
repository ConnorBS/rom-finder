# app/routers/ — Router Patterns & Gotchas

## Downloads & Pending Approval Flow

### Status lifecycle
`pending` → `downloading` → `hashing` → `verifying` (if RA creds set) → `pending_approval` (if check_dir on) or `completed`

### Automatic pipeline (`_run_download` in `downloads.py`)
1. File saved to `check_dir` or `download_dir`
2. Status → `hashing`: RAHasher binary first, falls back to Python MD5
3. Status → `verifying` (only if `ra_username` + `ra_api_key` configured)
4. Status → `pending_approval` or `completed`

### Pending approval card UI
- Truncated hash value (or "Not hashed" if missing)
- "RA verified" link to `https://retroachievements.org/game/{id}` if matched, else "Not in RA database"
- **Hash button**: visible only when `file_hash is None` → `POST /downloads/{id}/hash`
- **Check RA button**: visible when `file_hash` exists + `not hash_verified` + `ra_configured` → `POST /downloads/{id}/verify-ra`
- Approve / Reject buttons

### Manual hash endpoint (`POST /downloads/{id}/hash`)
Sets status → `hashing`, kicks `_run_hash()` background task. Task stores hash, clears `hash_verified`, returns to `pending_approval`.

### Manual RA verify endpoint (`POST /downloads/{id}/verify-ra`)
Sets status → `verifying`, kicks `_run_verify_ra()`. Task calls `lookup_hash()`, sets `hash_verified` + `ra_game_id` on match, returns to `pending_approval`. Returns yellow warning if RA creds missing.

---

## Cover Refresh Endpoints

- `POST /wanted/{id}/refresh-cover` (`wanted.py`) — deletes file, clears `WantedGame.cover_path`, re-queues fetch
- `POST /library/{id}/refresh-cover` (`collection.py`) — same for `LibraryEntry`
- Response replaces the button with disabled "Fetching…"; card-states overlay shows progress
- Button only renders when `covers_enabled` is True (at least one source configured)

---

## Router Gotchas

### `ra_configured` template context
All endpoints rendering `download_item.html` must pass `ra_configured = bool(ra_username and ra_api_key)`. Missing it silently hides the "Check RA" button.

### `_ACTIVE_STATUSES` in `activity.py` router
Must include `DownloadStatus.verifying` so the sidebar tray shows RA-lookup progress. If a new in-progress `DownloadStatus` is added, add it here too.

### Collection `no_ra` filter
`status == "no_ra"` is a special filter condition handled separately from the four real statuses. Backend checks: `file_hash` is set AND `ra_matched` is False. Not a DB column value.

### ROM scan is recursive
All three scan paths (`bulk_scan`, `/library/scan`, `run_scan` in scheduler) use `subdir.rglob('*')` to find ROMs nested inside system subfolders (e.g. `NES/No-Intro/game.nes`). System name is always the top-level folder under `download_dir`.

### Bulk actions: scope model
Collection bulk actions (Hashes, Verify) are dropdowns scoped two ways: **Filtered view** (passes `library_ids` = the current filtered set, capped at 500 via `all_filtered_lib_ids`) and **Entire library** (no `library_ids`). **Verify** (`/collection/bulk/verify`) additionally **skips already-RA-matched ROMs by default** (`include_matched=false`) so it doesn't re-hammer RA for verified games; `include_matched=true` re-verifies them. `_do_verify` stops early on a 429 (`SourceRateLimitError`) instead of hammering — the rest is left to retry or the scheduled resumable re-verify.

### Scan = sync (Collection `bulk_scan`)
The Collection "Scan folder" button is a 3-way sync. It reports scope: *"Scanned N files across M folders — X imported, Y marked missing, Z restored."*
- **Import** new ROM files on disk.
- **Flag missing** (soft, `LibraryEntry.missing` / `missing_at`, migration 0012) entries whose file left disk — NOT deleted. Safety guard: if >half the present library (and >5) is missing, assume an unmounted drive and **skip flagging**, logging a warning.
- **Resurrect** a missing entry automatically if its file reappears (`missing` cleared).

Missing entries show in the collection with a `⊘ Missing` badge + a `missing` filter, and two actions: **Delete** (`POST /collection/library/{id}/delete`, permanent) and **→ Wanted** (`POST /collection/library/{id}/to-wanted`, creates/resets the wanted game to `hunting` and removes the entry; requires an `ra_game_id`). Scan is metadata only — no hashing/verifying (scheduler Hash/RA-verify tasks). `/library/scan` and scheduler `run_scan` don't flag missing yet — only `bulk_scan`.

### Archive support (.zip/.7z)
Both in `ROM_EXTENSIONS`, scanned like any ROM. Hashing extracts to temp dir, hashes the ROM-like file inside (prefers a member matching the expected name, else largest — see `prefer_name` in `hasher.py`), cleans up — archive stays on disk. RAHasher handles zips natively; Python fallback uses `_hash_from_archive` in `hasher.py`. `_rom_title()` in `library.py` strips inner extension: `game.nes.zip` → title `game`.

### Source errors are surfaced, not swallowed (Phase 4)
Search endpoints (`games.search`, `api.api_search`) query each enabled source in its own try/except: one source's 403/429/dead-mirror failure is logged + shown in the results partial, but never aborts the others or hides as "no results". Sources raise the typed errors in `app/services/sources/errors.py`.

---

## Agent-Observable Diagnostics (`api.py`)

Two JSON endpoints let any agent confirm the running app's state over HTTP — no browser, no Docker socket, no human checking the site. They are the agent-facing twins of the UI; the verifier agents (`deploy-verifier`, `prod-health-monitor`) consume them instead of scraping HTML.

- **`GET /api/status`** → `{version, rahasher:{available,path,bin}, db:{library_total, library_ra_matched, no_ra, library_unhashed, wanted_*, downloads_*}, scheduler:{scan,hash,autodiscover → {enabled,time,last_run}}, verify:{in_progress,paused_until,last_run}, sources[], extensions[], recent_errors:{window_hours,count,latest[]}}`. `version` is `APP_VERSION` (the deployed git SHA). Each section is independently `try`-guarded — a failing section returns `{"error": "..."}` instead of blanking the whole report.
- **`GET /api/logs?level=&category=&since=&limit=`** → JSON array of `AppLog` rows (newest first; `limit` 1–1000, `since` is ISO-8601 UTC).

**Rule:** anything that previously only went to Docker stdout (RAHasher availability in `main.py`, extension-load failures in `extension_loader.py`) must ALSO go through `applog` so it shows up here. Don't add a diagnostic signal that's only visible in stdout.

`recent_errors.window_hours` comes from the `diagnostics_recent_hours` setting (default 24).
