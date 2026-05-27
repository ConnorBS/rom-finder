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
added_at
```

### `WantedGame` (table: `wanted_games`)
Games the user is hunting for.
```
id, game_title, system
ra_game_id       — RA game ID (required)
cover_path       — same format as LibraryEntry
status           — HuntStatus: hunting | verified
added_at, updated_at
```

### `Download` (table: `download`)
Active/completed download queue entries.
```
id, game_title, system, file_name, file_path
source_url, source_id, archive_identifier
status           — DownloadStatus enum:
                     pending | downloading | hashing | verifying | completed | failed | verified | pending_approval
progress         — 0.0–1.0
file_hash, hash_verified, ra_game_id
error_message, created_at, updated_at
```

### `AppSetting` (table: `app_settings`)
Simple key/value store. Defaults seeded in `main.py::DEFAULT_SETTINGS`.

### `AppLog` (table: `app_logs`)
Structured log: `ts, level, category, message, details (JSON)`.

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

---

## Settings Keys

```
download_dir            — root ROMs directory (system subfolders inside)
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

---

## Repository (`repository.py`)

A deliberately small data-access seam — **NOT** a general ORM wrapper. Functions take an explicit `session` (caller controls lifetime; never held across an await) and is a leaf module (imports only sqlmodel + models).

Current functions (each existed in 3+ near-identical copies before extraction):
- `wanted_by_ra_game_id(session, ra_game_id)` — the most-duplicated query (downloads ×3, api, hunter).
- `mark_wanted_verified(session, ra_game_id)` — find wanted → set verified + touch `updated_at`; no-op when id is None/unmatched. Used by all three download-approval paths.
- `create_library_entry_from_download(session, download, file_path, file_hash=None)` — the LibraryEntry construction copy-pasted into approve / approve-all / `_run_download`.
- `unverified_library_entries(session)` — `file_hash IS NOT NULL AND ra_matched == False`; the `no_ra` set + the Phase 5 resumable-verify work set.
- `library_pending_ra_check(session, stale_days, limit, exclude_systems=None)` — the resumable-verify work set. `exclude_systems` (a set of system names) drops platforms RA can't verify; the caller passes `ra_client.RA_UNSUPPORTED_SYSTEMS` so this module stays db-pure (no `app.services` import).

**Rule:** only add a function here when a query/mutation repeats 3+ times AND drift risks correctness. Simple one-off queries (and genuinely different ones, e.g. hunter's mark-by-primary-key that sets `last_hunt_at`) stay inline.

### `hashed_at` backfill gotcha
Entries hashed before `hashed_at` was added have `file_hash != None, hashed_at = None`. The scheduler hash-check backfills these on first run.
