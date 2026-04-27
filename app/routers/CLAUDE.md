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

### Archive support (.zip/.7z)
Both in `ROM_EXTENSIONS`, scanned like any ROM. Hashing extracts to temp dir, hashes the largest ROM-like file inside, cleans up — archive stays on disk. RAHasher handles zips natively; Python fallback uses `_hash_from_archive` in `hasher.py`. `_rom_title()` in `library.py` strips inner extension: `game.nes.zip` → title `game`.
