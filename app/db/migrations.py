"""Ordered, tracked schema migrations.

Not Alembic — same add-column style as before, but each migration has a stable
id, runs in order, and is recorded in `schema_migrations` so it only runs once.
Column-adds stay idempotent (PRAGMA guard) so a DB that predates the tracking
table (prod already has the first five columns) back-fills cleanly: each
existing migration's PRAGMA check no-ops, then its id is recorded.

Add a new migration by appending `(id, apply_fn)` to MIGRATIONS. Never reorder
or rename existing ids.
"""

from sqlmodel import Session, text

from app.db.database import engine


def _column_exists(session: Session, table: str, col: str) -> bool:
    rows = session.exec(text(f"PRAGMA table_info({table})")).all()
    return col in {r[1] for r in rows}


def _add_column(session: Session, table: str, col: str, col_type: str, default: str | None) -> None:
    if _column_exists(session, table, col):
        return
    if default is not None:
        session.exec(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default}"))
    else:
        session.exec(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))


def _m_0001(s: Session) -> None:
    _add_column(s, "download", "source_id", "VARCHAR", "'archive_org'")


def _m_0002(s: Session) -> None:
    _add_column(s, "download", "ra_game_id", "INTEGER", None)


def _m_0003(s: Session) -> None:
    _add_column(s, "library", "cover_path", "VARCHAR", "''")


def _m_0004(s: Session) -> None:
    _add_column(s, "library", "hashed_at", "TIMESTAMP", None)


def _m_0005(s: Session) -> None:
    _add_column(s, "wanted_games", "last_hunt_at", "TIMESTAMP", None)


def _m_0006_wanted_unique(s: Session) -> None:
    # De-dupe (ra_game_id, system) keeping the lowest id, THEN enforce uniqueness.
    # api.py deduped in Python which races under concurrent extension posts.
    s.exec(text(
        "DELETE FROM wanted_games WHERE id NOT IN "
        "(SELECT MIN(id) FROM wanted_games GROUP BY ra_game_id, system)"
    ))
    s.exec(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_wanted_ra_system "
        "ON wanted_games(ra_game_id, system)"
    ))


def _m_0007_download_path_unique(s: Session) -> None:
    # Partial unique on non-null file_path — stops two concurrent downloads
    # writing the same destination.
    s.exec(text(
        "DELETE FROM download WHERE file_path IS NOT NULL AND id NOT IN "
        "(SELECT MIN(id) FROM download WHERE file_path IS NOT NULL GROUP BY file_path)"
    ))
    s.exec(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_download_path "
        "ON download(file_path) WHERE file_path IS NOT NULL"
    ))


def _m_0008_library_path_unique(s: Session) -> None:
    # Enables idempotent "already imported?" checks for bulk import (Phase 9).
    s.exec(text(
        "DELETE FROM library WHERE id NOT IN "
        "(SELECT MIN(id) FROM library GROUP BY file_path)"
    ))
    s.exec(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_library_path ON library(file_path)"
    ))


def _m_0009_library_ra_checked_at(s: Session) -> None:
    # When the entry was last looked up against RA — lets the resumable verify
    # skip recently-checked genuine misses so passes terminate and the daily
    # scheduler pass doesn't re-hammer the whole no_ra set.
    _add_column(s, "library", "ra_checked_at", "TIMESTAMP", None)


def _m_0010_normalize_system_names(s: Session) -> None:
    # One-time fix for system names the Chrome extension doubled while scraping
    # RA link text ("WiiWii"). New rows are normalized server-side in api.py
    # (title_utils.canonical_system), so this replaces the every-startup UPDATE
    # that used to live in main.py lifespan.
    for table in ("wanted_games", "library"):
        s.exec(text(f"UPDATE {table} SET system = 'Wii' WHERE system = 'WiiWii'"))


def _m_0011_hunt_attempt_source_url(s: Session) -> None:
    # Record the resolved download URL on each hunt attempt — a stable per-file
    # identity for dedup (don't re-download the same URL) and so users can see
    # exactly what was attempted.
    _add_column(s, "hunt_attempts", "source_url", "VARCHAR", "''")


def _m_0012_library_missing(s: Session) -> None:
    # Soft "missing" flag for library entries whose file left disk — flagged
    # (with options to delete / move to wanted), not hard-deleted, and resurrected
    # automatically if the ROM reappears on a rescan.
    _add_column(s, "library", "missing", "BOOLEAN", "0")
    _add_column(s, "library", "missing_at", "TIMESTAMP", None)


def _m_0013_normalize_wii(s: Session) -> None:
    # Folder "Nintendo Wii" mapped to a non-canonical system "Nintendo Wii" before
    # DEFAULT_FOLDER_MAP was fixed, splitting Wii into two groups. Canonicalize to
    # "Wii" (RA's name) so it matches and displays as one console.
    for table in ("library", "wanted_games"):
        s.exec(text(f"UPDATE {table} SET system = 'Wii' WHERE system = 'Nintendo Wii'"))


def _m_0014_library_duplicate_of(s: Session) -> None:
    # Tag redundant library copies: a duplicate's canonical sibling id (same content
    # by hash, or same title+system). Recomputed by services/duplicates.py;
    # NULL = canonical/unique.
    _add_column(s, "library", "duplicate_of", "INTEGER", None)


def _m_0015_library_saves(s: Session) -> None:
    # Read-only save-file detection: which games have an emulator save, matched by
    # filename stem. Populated by services/saves.py::scan_saves; never edited/deleted.
    _add_column(s, "library", "save_count", "INTEGER", "0")
    _add_column(s, "library", "save_files", "VARCHAR", "''")
    _add_column(s, "library", "save_updated_at", "TIMESTAMP", None)


def _m_0016_library_disc_id(s: Session) -> None:
    # 4-char Wii title-ID-low (e.g. "RKME") — keys Dolphin NAND save matching, since
    # Dolphin stores Wii saves under User/Wii/title/{type}/<hex(title_id_low)>/data/
    # (not by ROM filename). Populated lazily from the disc header by services/saves.
    _add_column(s, "library", "disc_id", "VARCHAR", "''")


def _m_0017_library_awards_subsets(s: Session) -> None:
    # RetroAchievements awards + hash-aware subsets:
    #  - ra_award: own highest RA award tier (mastered|completed|beaten|beaten-softcore|"")
    #    from the dashboard mirror — drives the Mastered/Completed/Beaten badges.
    #  - is_subset_rom: this entry is itself an RA "Subset" copy.
    #  - subset_info: JSON of subsets whose accepted hash list contains this ROM's hash
    #    (RA-backed cache in ra_subset_hash) — drives the "subset available" marker.
    # All derived/read-only; populated by services/mastery.py + services/subsets.py.
    _add_column(s, "library", "ra_award", "VARCHAR", "''")
    _add_column(s, "library", "is_subset_rom", "BOOLEAN", "0")
    _add_column(s, "library", "subset_info", "VARCHAR", "''")


def _m_0018_library_file_size(s: Session) -> None:
    # ROM file size in bytes — enables sorting the collection by size (e.g. largest
    # mastered games to delete for space). Populated/refreshed on scan + rehash.
    _add_column(s, "library", "file_size", "INTEGER", "0")


def _m_0019_download_hunt_task_id(s: Session) -> None:
    # Ties a transient Download row created by an auto-hunt attempt to its activity
    # task ("hunt-{wanted_id}") so the download card can offer a Cancel that targets
    # the hunt. NULL for normal manual/queue downloads.
    _add_column(s, "download", "hunt_task_id", "VARCHAR", None)


def _m_0020_goal_achievement_desc(s: Session) -> None:
    # Achievement goals cache the achievement's description (fetched from the RA API)
    # so the goal card explains what the achievement is. The `goal` table was created
    # by create_all, so this column-add is needed for already-deployed DBs.
    _add_column(s, "goal", "achievement_desc", "VARCHAR", "''")


def _m_0021_goal_points(s: Session) -> None:
    # Achievement goals cache the achievement's RA point value; the Goals page sums
    # these per event for the event header's point total.
    _add_column(s, "goal", "points", "INTEGER", "0")


def _m_0022_goalevent_tiers(s: Session) -> None:
    # Cache an event's RA V2 award tiers (Bronze/Silver/Gold/Champion + pointsRequired
    # + badge) as JSON on the GoalEvent. goal_events was created by create_all in an
    # earlier deploy, so the column-add is needed for existing DBs.
    _add_column(s, "goal_events", "tiers_json", "VARCHAR", "''")


def _m_0023_library_time_to_beat(s: Session) -> None:
    # RA V2 medianTimeToBeatMinutes per owned game; captured by game_sets.refresh_game_sets.
    _add_column(s, "library", "time_to_beat_min", "INTEGER", "0")


def _m_0024_library_chd_codec(s: Session) -> None:
    # CHD container codec status from the file header (services/chd_format.run_chd_check):
    # "" = not a CHD/unchecked, "ok" = RA-safe, "cdzs"/"zstd" = Zstandard (RetroArch can't
    # hash it → no achievements until re-encoded). Drives the bad_chd filter + /api/status.chd.
    _add_column(s, "library", "chd_codec", "VARCHAR", "''")


def _m_0026_goal_custom_display(s: Session) -> None:
    # Goal sub-category + custom card art (services/routers goals). The `goal` table was
    # created by create_all, so these column-adds are needed for already-deployed DBs.
    # GoalStatus gains "failed" but `status` is already a VARCHAR — no column change.
    # goal_categories is a new table (create_all), so no migration for it.
    _add_column(s, "goal", "category", "VARCHAR", "''")
    _add_column(s, "goal", "custom_image", "VARCHAR", "''")
    _add_column(s, "goal", "display_text", "VARCHAR", "''")
    _add_column(s, "goal", "icon", "VARCHAR", "''")
    _add_column(s, "goal", "icon_color", "VARCHAR", "''")


def _m_0025_library_root_id(s: Session) -> None:
    # Which registered LibraryRoot (directory) a ROM lives in. NULL until backfilled by
    # services/library_roots.ensure_primary_and_backfill (lifespan), which longest-prefix
    # matches each entry's file_path to a root. Enables the directory badge/filter,
    # cross-directory duplicate detection, and move-source resolution. The library_roots
    # table itself is created by create_all (new table, no migration needed).
    _add_column(s, "library", "root_id", "INTEGER", None)


# (version_id, apply_fn) — applied in order, recorded once.
MIGRATIONS: list[tuple[str, "callable"]] = [
    ("0001_download_source_id", _m_0001),
    ("0002_download_ra_game_id", _m_0002),
    ("0003_library_cover_path", _m_0003),
    ("0004_library_hashed_at", _m_0004),
    ("0005_wanted_last_hunt_at", _m_0005),
    ("0006_wanted_unique", _m_0006_wanted_unique),
    ("0007_download_path_unique", _m_0007_download_path_unique),
    ("0008_library_path_unique", _m_0008_library_path_unique),
    ("0009_library_ra_checked_at", _m_0009_library_ra_checked_at),
    ("0010_normalize_system_names", _m_0010_normalize_system_names),
    ("0011_hunt_attempt_source_url", _m_0011_hunt_attempt_source_url),
    ("0012_library_missing", _m_0012_library_missing),
    ("0013_normalize_wii", _m_0013_normalize_wii),
    ("0014_library_duplicate_of", _m_0014_library_duplicate_of),
    ("0015_library_saves", _m_0015_library_saves),
    ("0016_library_disc_id", _m_0016_library_disc_id),
    ("0017_library_awards_subsets", _m_0017_library_awards_subsets),
    ("0018_library_file_size", _m_0018_library_file_size),
    ("0019_download_hunt_task_id", _m_0019_download_hunt_task_id),
    ("0020_goal_achievement_desc", _m_0020_goal_achievement_desc),
    ("0021_goal_points", _m_0021_goal_points),
    ("0022_goalevent_tiers", _m_0022_goalevent_tiers),
    ("0023_library_time_to_beat", _m_0023_library_time_to_beat),
    ("0024_library_chd_codec", _m_0024_library_chd_codec),
    ("0025_library_root_id", _m_0025_library_root_id),
    ("0026_goal_custom_display", _m_0026_goal_custom_display),
]


def _ensure_tracking_table(session: Session) -> None:
    session.exec(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version VARCHAR PRIMARY KEY, "
        "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    ))


def _applied_versions(session: Session) -> set[str]:
    rows = session.exec(text("SELECT version FROM schema_migrations")).all()
    return {r[0] for r in rows}


def run_migrations() -> None:
    """Apply any migrations not yet recorded in schema_migrations, in order."""
    with Session(engine) as session:
        _ensure_tracking_table(session)
        applied = _applied_versions(session)
        for version, apply_fn in MIGRATIONS:
            if version in applied:
                continue
            apply_fn(session)
            session.exec(text(f"INSERT INTO schema_migrations (version) VALUES ('{version}')"))
        session.commit()
