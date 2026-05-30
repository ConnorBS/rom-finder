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
`status == "no_ra"` is a special filter condition handled separately from the four real statuses. Backend checks: `file_hash` is set AND `ra_matched` is False AND **not unsupported**. Not a DB column value.

### Collection `duplicate` filter + tag
`status == "duplicate"` filters to entries whose `LibraryEntry.duplicate_of` is set —
redundant copies (same content by hash, or same title+system — `ra_game_id` is NOT used, since RA files many distinct ROMs under one id). Computed by
`services/duplicates.py::recompute_duplicates` (full rebuild; LOCAL, no RA calls), which
is auto-run after `bulk_scan`/`_do_rehash`/`_do_verify`/scheduler `run_scan`, and on demand
via **`POST /collection/recompute-duplicates`** (the "Find duplicates" button). The fix that
made archive-wrapped discs hashable (`rahasher` unwrap) is what lets a `.7z`/`.zip` copy match
its `.cue` sibling's hash and get tagged. `.bin`/`.img` tracks and different discs are never
tagged (subsets — deleting a track would break its `.cue`). Card view shows a purple
`⧉ Duplicate` badge; `/api/status.db.duplicates` carries the count.

### Collection `has_save` filter + save detection (read-only)
`status == "has_save"` filters to entries with `LibraryEntry.save_count > 0`. Saves are matched to ROMs by filename stem in `services/saves.py::scan_saves`, run after scan/folder-scan and via **`POST /collection/scan-saves`** (the "Scan saves" button). A 💾 **Save** badge shows on cards, a header count ("N with save"), and the card detail slide-over lists the actual save filenames + kind (battery/state). **There is deliberately NO save edit/delete UI or endpoint — saves are read-only.** The delete-file action (`_delete_rom_file`) only removes ROM/disc-track extensions, never `.srm`/`.sav`/`.state`/etc.

### Collection `unsupported` filter (platforms RA can't verify)
`status == "unsupported"` is another computed filter: the entry's `system` is in `ra_client.RA_UNSUPPORTED_SYSTEMS` (curated set, e.g. `Nintendo 3DS`, `Archipelago` — RA has no console for them, so they can NEVER hash-match). These are **excluded from the `no_ra` count/filter** (so they stop looking like failures) and get a slate `⊘ No RA platform` badge + an "Unsupported" filter chip + a count in the header bar. Every verify path skips them so RA is never called for an unverifiable platform: the resumable verify (`library_pending_ra_check(exclude_systems=...)`), `bulk_verify` (SQL `system.not_in(...)`), and the single `/library/{id}/verify-ra` (early return). Curated, not derived from `SYSTEMS` — misnamed-but-supported folders like `tg16`/`mega-duck-slash-cougar-boy` do verify, so a "not in SYSTEMS" heuristic would wrongly hide real matches.

### ROM scan is recursive
All three scan paths (`bulk_scan`, `/library/scan`, `run_scan` in scheduler) use `subdir.rglob('*')` to find ROMs nested inside system subfolders (e.g. `NES/No-Intro/game.nes`). System name is always the top-level folder under `download_dir`.

### Disc tracks are not separate ROMs (`is_disc_track` in `library.py`)
A disc rip is one logical game: a `.cue`/`.gdi` (the canonical, hashable entry) plus one or more `.bin`/`.img` tracks (incl. audio). `is_disc_track(f, cue_cache)` returns True for a `.bin`/`.img` that has a sibling cuesheet, and **all three scan paths skip those** — otherwise each track imports as its own entry and clutters no_ra (they can never RA-hash-match). This surfaces when a disc is **unzipped** (e.g. for an Android handheld) so the recursive walk hits each track file. `bulk_scan` additionally **deletes already-imported track artifacts** (unmatched `.bin`/`.img` with a sibling `.cue`, file still on disk) and reports "N disc-track artifacts removed". The `cue_cache` dict memoises the per-folder cuesheet check so big folders aren't globbed once per file.

### Bulk actions: scope model
Collection bulk actions (Hashes, Verify) are dropdowns scoped two ways: **Filtered view** (passes `library_ids` = the current filtered set, capped at 500 via `all_filtered_lib_ids`) and **Entire library** (no `library_ids`). **Verify** (`/collection/bulk/verify`) additionally **skips already-RA-matched ROMs by default** (`include_matched=false`) so it doesn't re-hammer RA for verified games; `include_matched=true` re-verifies them. It also excludes `RA_UNSUPPORTED_SYSTEMS` (SQL `not_in`) so RA is never called for unverifiable platforms. `_do_verify` stops early on a 429 (`SourceRateLimitError`) instead of hammering — the rest is left to retry or the scheduled resumable re-verify. **Commit discipline:** it opens a FRESH session per entry and commits each result immediately (never holds a session across the `lookup_hash` await) — so `no_ra` drops live, cards clear as they go, and a mid-batch restart loses at most the in-flight entry instead of the whole batch. It stamps `ra_checked_at` per entry so the resumable nightly pass won't re-check them until stale.

### Scan = sync (Collection `bulk_scan`)
The Collection "Scan folder" button is a 3-way sync. It reports scope: *"Scanned N files across M folders — X imported, Y marked missing, Z restored."*
- **Import** new ROM files on disk.
- **Flag missing** (soft, `LibraryEntry.missing` / `missing_at`, migration 0012) entries whose file left disk — NOT deleted. Safety guard: if >half the present library (and >5) is missing, assume an unmounted drive and **skip flagging**, logging a warning.
- **Resurrect** a missing entry automatically if its file reappears (`missing` cleared).

Missing entries show in the collection with a `⊘ Missing` badge + a `missing` filter, and two actions: **Delete** (`POST /collection/library/{id}/delete`) and **→ Wanted** (`POST /collection/library/{id}/to-wanted`, creates/resets the wanted game to `hunting` and removes the entry; requires an `ra_game_id`). Scan is metadata only — no hashing/verifying (scheduler Hash/RA-verify tasks). `/library/scan` and scheduler `run_scan` don't flag missing yet — only `bulk_scan`.

### Deleting library entries (entry vs entry + file)
`POST /collection/library/{id}/delete` removes the DB entry. With **`?delete_file=true`** it also deletes the ROM **file** from disk (and, for a `.cue`/`.gdi`, its same-stem `.bin`/`.img`/`.iso`/`.sub` tracks via `_delete_rom_file`) — **refused** (entry kept, reason returned) if the file's root (`download_dir`/`check_dir`) is locked read-only. On success it returns an empty body with **`HX-Refresh: true`** (grid + counts update, slide-over resets) and re-runs `recompute_duplicates` (a group may collapse / re-elect its canonical). Surfaced two ways: the **list view** Actions column (Remove / Delete file), and the **card detail slide-over** (`/library/{id}/detail`), which also renders the full **duplicate group** — every copy that shares this entry's canonical, listed **neutrally** (no "Kept" / "dup" labels — the user picks which to remove). The card badge appears on **every** group member (canonical included): `_build_collection` sets `item.duplicate = (id in in_dup_group)` where `in_dup_group` is the union of canonical ids that have dependents and entries with `duplicate_of` set. Errors render into the panel's `#dz-feedback`.

### Archive support (.zip/.7z)
Both in `ROM_EXTENSIONS`, scanned like any ROM. Hashing extracts to temp dir, hashes the ROM-like file inside (prefers a member matching the expected name, else largest — see `prefer_name` in `hasher.py`), cleans up — archive stays on disk. RAHasher handles zips natively; Python fallback uses `_hash_from_archive` in `hasher.py`. `_rom_title()` in `library.py` strips inner extension: `game.nes.zip` → title `game`.

### Source errors are surfaced, not swallowed (Phase 4)
Search endpoints (`games.search`, `api.api_search`) query each enabled source in its own try/except: one source's 403/429/dead-mirror failure is logged + shown in the results partial, but never aborts the others or hides as "no results". Sources raise the typed errors in `app/services/sources/errors.py`.

---

## Agent-Observable Diagnostics (`api.py`)

Two JSON endpoints let any agent confirm the running app's state over HTTP — no browser, no Docker socket, no human checking the site. They are the agent-facing twins of the UI; the verifier agents (`deploy-verifier`, `prod-health-monitor`) consume them instead of scraping HTML.

- **`GET /api/status`** → `{version, rahasher:{available,path,bin}, db:{library_total, library_ra_matched, no_ra, library_unhashed, wanted_*, downloads_*}, scheduler:{scan,hash,autodiscover → {enabled,time,last_run}}, verify:{in_progress,paused_until,last_run}, dashboard:{achievements,games,last_sync}, sources[], extensions[], recent_errors:{window_hours,count,latest[]}}`. `version` is `APP_VERSION` (the deployed git SHA). `dashboard` reports the RA mirror's local counts + last manual sync. Each section is independently `try`-guarded — a failing section returns `{"error": "..."}` instead of blanking the whole report.
- **`GET /api/logs?level=&category=&since=&limit=`** → JSON array of `AppLog` rows (newest first; `limit` 1–1000, `since` is ISO-8601 UTC).

**Rule:** anything that previously only went to Docker stdout (RAHasher availability in `main.py`, extension-load failures in `extension_loader.py`) must ALSO go through `applog` so it shows up here. Don't add a diagnostic signal that's only visible in stdout.

`recent_errors.window_hours` comes from the `diagnostics_recent_hours` setting (default 24).

---

## RetroAchievements Dashboard (`dashboard.py`)

`/dashboard` pages (Overview, `/timeline`, `/games`, `/insights`, `/reports`) read **only the local mirror** (`ra_dashboard.py` query helpers) — zero RA calls on page load. The single RA-touching action is **`POST /dashboard/refresh`**, which kicks `ra_dashboard.refresh()` as a `BackgroundTask` (progress in the activity tray as `ra-sync`) and returns a feedback snippet; the user reloads when it finishes.

- Filter pages (Timeline, Games) use **plain GET forms** (full re-render) rather than HTMX partial-swaps — that keeps ApexCharts init bulletproof (no re-init-on-swap).
- Charts get their series as `{{ ... | tojson }}` injected into an inline init script per page (ApexCharts loaded globally in `base.html`).
- **Reports**: `GET /dashboard/reports/preview` (hx-get → read-only textarea + Copy/Download) and `GET /dashboard/reports/download` (StreamingResponse `text/markdown`) both call `ra_report.build(...)`. Report types: `recap|lifetime|per_game|custom`.
- Empty-state CTAs render when the mirror is empty or RA creds are missing (`_ra_configured`).
