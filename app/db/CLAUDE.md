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

SQLite doesn't support dropping columns. New columns added via `main.py::_MIGRATIONS`:

```python
_MIGRATIONS = [
    ("table_name", "column_name", "SQL_TYPE", "DEFAULT_EXPR or None"),
]
```

- `None` default = nullable column
- Non-null defaults use a SQL expression string: `"''"`, `"0"`, etc.
- `_run_migrations()` runs at startup via `lifespan`
- **Never use Alembic**

### `hashed_at` backfill gotcha
Entries hashed before `hashed_at` was added have `file_hash != None, hashed_at = None`. The scheduler hash-check backfills these on first run.
