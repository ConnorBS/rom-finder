# app/db/ — Data Models, Migrations & Settings

## Data Models (`models.py`)

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
duplicate_of     — canonical sibling's library id when this entry is a redundant copy
                   (same content by hash, or same title+system); None = canonical/unique (migration 0014)
save_count       — # of emulator save files matched to this ROM (migration 0015); >0 = has a save
save_files       — JSON [{name,kind,size,mtime}] of matched saves; kind = battery|state
save_updated_at  — newest matched save's mtime
disc_id          — 4-char Wii title-ID-low from the disc header (migration 0016); keys Dolphin NAND saves
ra_award         — own highest RA award tier (migration 0017): mastered|completed|beaten|beaten-softcore|""
is_subset_rom    — this entry is itself an RA "Subset" copy (title/filename marker)
subset_info      — JSON [{game_id,title,mastered}] of subsets whose accepted hash list contains this ROM's hash
file_size        — ROM file size in bytes (migration 0018); enables the collection size sort + the detail-panel Size row; set on scan/rehash AND on download-approval (create_library_entry_from_download stats the file)
added_at
```

`save_*` are derived (read-only) — `services/saves.py::scan_saves` matches saves to ROMs
by filename stem and rebuilds them. **ROM Finder never edits or deletes a save**; these
columns are display-only.

`ra_award` / `is_subset_rom` / `subset_info` are derived (read-only). `ra_award` is set by
`services/mastery.py::sync_library_awards` from the dashboard mirror (LOCAL; "mastered" =
hardcore 100%). `is_subset_rom` + `subset_info` are set by `services/subsets.py::recompute_subset_flags`
(LOCAL) from the `ra_subset_hash` cache — a ROM matches a subset purely by hash, so a subset
needing a patched ROM the user doesn't own never matches.

`duplicate_of` is derived, not authoritative — `services/duplicates.py::recompute_duplicates`
fully rebuilds it (clear + re-derive) after any scan/rehash/verify pass. A `.bin`/`.img`
track and different discs of a multi-disc game are never tagged (they're subsets).

### `WantedGame` (table: `wanted_games`)
Games the user is hunting for.
```
id, game_title, system
ra_game_id       — RA game ID (required)
cover_path       — same format as LibraryEntry
status           — HuntStatus: hunting | verified
added_at, updated_at
```

### `Goal` (table: `goal`)
Per-game event objectives the user tracks.
```
id, game_title, system
ra_game_id       — RA game ID; None for custom/non-RA goals
cover_path       — "covers/{ra_game_id}.png" (reuses any cover already on disk)
objective        — GoalObjective: master | beaten | custom
custom_text      — freeform objective label (e.g. "finish level 5"); custom only
event_name       — free-text grouping label ("" = ungrouped); indexed
deadline         — midnight-UTC datetime of the target day; None = no deadline
status           — GoalStatus: active | completed
auto             — True once the RA evaluator (not the user) flipped it done
created_at, updated_at, completed_at
```
**New table → created by `create_all` at startup; NO migration** (migrations are only for
ALTER/index on existing tables). **`master`/`beaten` are HARDCORE-only** — `master` needs
`highest_award_kind == "mastered"`, `beaten` needs `in ("beaten","mastered")`; softcore
awards (`beaten-softcore`/`completed`) never satisfy a goal. Auto-completion is LOCAL
(`services/goals.py::evaluate_goals`, run after a dashboard refresh + on every Goals page
load). **Progress/award are NOT cached on the row** — the page joins live to `RAGameProgress`
by `ra_game_id` at render time (the mirror is replaced wholesale on refresh, so a cached copy
would drift). Deadline parsed from an `<input type="date">` (`YYYY-MM-DD`); empty → None.

### `Download` (table: `download`)
Active/completed download queue entries.
```
id, game_title, system, file_name, file_path
source_url, source_id, archive_identifier
status           — DownloadStatus enum:
                     pending | downloading | hashing | verifying | completed | failed | verified | pending_approval | moving
                     (moving = approved, file being moved to the ROMs dir by a background task)
progress         — 0.0–1.0
file_hash, hash_verified, ra_game_id
hunt_task_id     — set to "hunt-{wanted_id}" on a TRANSIENT row an auto-hunt creates
                   per download attempt (live progress card + Cancel); None for a
                   normal manual/queue download. (migration 0019) Cleared when the row
                   reaches a terminal state (verify reuses the row → pending_approval).
error_message, created_at, updated_at
```
`HuntStatus`: `hunting | verified | exhausted | awaiting_external` (last = submitted to a torrent/usenet download client, polled to completion — Phase 2).

### `ExternalDownload` (table: `external_download`)
A torrent/usenet job submitted to a download client (qBittorrent/SABnzbd via Prowlarr) as a **last-resort** hunt fallback. `wanted_game_id`, `download_id` (the linked progress `Download`), `client_id`, `protocol`, `job_handle` (qBit infohash / SAB nzo_id), `release_title`, `indexer`, `save_path`, `target_files` (JSON, selected filenames), `match_data` (JSON `{ra_stems,title_terms,accepted_md5s}` captured at submit so the poller verifies with **no extra RA call**), `needs_file_selection`, `status` (submitted→metadata→downloading→completed→verifying→verified | failed), `progress`. Polled by `scheduler.run_poll_external`. **New table → created by `create_all`; no migration.** See `app/services/CLAUDE.md` → *Torrent/Usenet download client*.

### `AppSetting` (table: `app_settings`)
Simple key/value store. Defaults seeded in `main.py::DEFAULT_SETTINGS`.

### `AppLog` (table: `app_logs`)
Structured log: `ts, level, category, message, details (JSON)`.

### RA Dashboard mirror (`ra_achievement`, `ra_game_progress`, `ra_profile`)
A **local mirror** of the configured RA user's data, powering the dashboard with zero RA calls while browsing. Populated by `app/services/ra_dashboard.py::refresh()` (manual only). **The refresh REPLACES `ra_achievement` + `ra_game_progress` wholesale each run** — that's how retroactive RA changes (repointed/removed/demoted achievements, backdated unlocks) reconcile; never treat the mirror as append-only. `ra_profile` is a single row (id=1). `ra_game_progress.owned` is set by matching `game_id` to a `LibraryEntry.ra_game_id` (the owned-library cross-link). New tables → created by `create_all` at startup; **no migration needed** (migrations here are only for ALTER/index on existing tables).

### RA subset hash cache (`ra_subset_hash`)
Cached map of each owned game's subsets → their accepted MD5s. Refreshed by `app/services/subsets.py::refresh_subset_cache` (the only RA-calling part — enumerates subsets from `get_game_list`, pulls `get_game_hashes_full`), replaced **wholesale** on a full sweep or **per-parent** when scoped. Rows: `parent_game_id, subset_game_id, subset_title, console_id, md5`. A library ROM is matched purely by `md5 == file_hash` (`parent_game_id` only scopes which subsets to fetch). Created by `create_all` at startup; **no migration needed**. The collection reads only the derived `LibraryEntry.subset_info`, so browsing makes zero RA calls.

---

## Collection Status Vocabulary

The `/collection` page unifies `LibraryEntry` and `WantedGame`. Each item gets a status:

| Status | Meaning |
|---|---|
| `verified` | WantedGame.status == verified — RA hash confirmed |
| `found` | Has a LibraryEntry linked by ra_game_id or title+system; not yet verified |
| `library` | LibraryEntry only, no WantedGame |
| `wanted` | WantedGame only, no LibraryEntry yet |

`no_ra` is a special **filter condition** (not a real status): `file_hash` set + `ra_matched` is False.
`duplicate` is another filter condition: `LibraryEntry.duplicate_of is not None`.
`has_mastered` (ra_award == "mastered"), `beaten` (ra_award in beaten/beaten-softcore/completed, not mastered),
and `subset_available` (a `subset_info` subset still unmastered) are filter conditions too.

---

## Settings Keys

```
download_dir            — root ROMs directory (system subfolders inside)
check_dir               — staging area for ROMs awaiting approval
covers_dir              — where cover PNGs are saved (default: static/covers)
saves_dir               — emulator save directory (optional); scanned READ-ONLY to flag
                          which games have a save. Saves beside ROMs are also detected.
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

**`sched_autodiscover_*` vs `ra_autodiscover_*`**: Old `ra_autodiscover_*` keys still exist in the DB but are unused. Scheduler uses `sched_autodiscover_*`. Don't remove the old keys.

---

## Schema Migrations

SQLite doesn't support dropping columns. Migrations live in **`app/db/migrations.py`** (a leaf module importing only `database.engine`), run at startup via `lifespan` → `run_migrations()`.

Each migration is `(version_id, apply_fn)` appended to `MIGRATIONS`, applied in order, and recorded once in the `schema_migrations(version, applied_at)` table:

```python
def _m_0006_my_change(s: Session) -> None:
    _add_column(s, "table_name", "column_name", "SQL_TYPE", "DEFAULT_EXPR or None")

MIGRATIONS = [..., ("0006_my_change", _m_0006_my_change)]
```

- Add-column helper `_add_column` is idempotent (PRAGMA `table_info` guard) — so a DB that predates the tracking table (prod has the first 5 columns, no `schema_migrations`) back-fills cleanly: the column-add no-ops, then the id is recorded.
- `None` default = nullable column; non-null defaults use a SQL expression string: `"''"`, `"0"`.
- Migrations can also create indexes / dedupe rows (see `0006+`), not just add columns.
- **Never reorder or rename existing version ids.**
- **Never use Alembic.**

### SQLite pragmas
`database.py` sets `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL` on every connect (via a `connect` event listener, SQLite-only). This lets the scheduler, a live download, and a bulk verify write concurrently without `database is locked`. WAL produces `*.db-wal` / `*.db-shm` sidecar files (already gitignored).

### Connection pool
The engine is configured `pool_size=20, max_overflow=40, pool_timeout=30, pool_pre_ping=True`. The pool only stays healthy because **no route holds a pooled connection across a network `await`** — search/hunt endpoints read what they need in a short session, release it, then do their httpx calls (see the `Depends(get_session)` pitfall note in `app/CLAUDE.md`). Bumping the pool is headroom, NOT the fix; a connection held across a slow await still starves it. These are async routes calling sync `session.exec()` in the single event-loop thread, so DB access is effectively single-threaded — a connection is "stuck" only while its coroutine is suspended at an await holding it.

---

## Repository (`repository.py`)

A deliberately small data-access seam — **NOT** a general ORM wrapper. Functions take an explicit `session` (caller controls lifetime; never held across an await) and is a leaf module (imports only sqlmodel + models).

Current functions (each existed in 3+ near-identical copies before extraction):
- `wanted_by_ra_game_id(session, ra_game_id)` — the most-duplicated query (downloads ×3, api, hunter).
- `mark_wanted_verified(session, ra_game_id)` — find wanted → set verified + touch `updated_at`; no-op when id is None/unmatched. Used by all three download-approval paths.
- `create_library_entry_from_download(session, download, file_path, file_hash=None)` — the LibraryEntry construction copy-pasted into approve / approve-all / `_run_download`. Stats the on-disk file to set `file_size` so a fresh download/approval is sized immediately (not 0 until the next scan).
- `unverified_library_entries(session)` — `file_hash IS NOT NULL AND ra_matched == False`; the `no_ra` set + the Phase 5 resumable-verify work set.
- `library_pending_ra_check(session, stale_days, limit, exclude_systems=None)` — the resumable-verify work set. `exclude_systems` (a set of system names) drops platforms RA can't verify; the caller passes `ra_client.RA_UNSUPPORTED_SYSTEMS` so this module stays db-pure (no `app.services` import).

**Rule:** only add a function here when a query/mutation repeats 3+ times AND drift risks correctness. Simple one-off queries (and genuinely different ones, e.g. hunter's mark-by-primary-key that sets `last_hunt_at`) stay inline.

### `hashed_at` backfill gotcha
Entries hashed before `hashed_at` was added have `file_hash != None, hashed_at = None`. The scheduler hash-check backfills these on first run.
