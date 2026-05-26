"""Phase 0: tracked migration registry + SQLite pragmas."""

from sqlmodel import Session, text

from app.db.migrations import MIGRATIONS, run_migrations


def _applied(engine) -> set[str]:
    with Session(engine) as s:
        return {r[0] for r in s.exec(text("SELECT version FROM schema_migrations")).all()}


def test_all_migrations_recorded(fresh_engine):
    applied = _applied(fresh_engine)
    assert applied == {v for v, _ in MIGRATIONS}


def test_run_migrations_is_idempotent(fresh_engine):
    # Re-running must not error and must not duplicate rows.
    run_migrations()
    run_migrations()
    with Session(fresh_engine) as s:
        count = s.exec(text("SELECT COUNT(*) FROM schema_migrations")).one()
    assert count[0] == len(MIGRATIONS)


def test_wal_pragma_active(fresh_engine):
    with Session(fresh_engine) as s:
        mode = s.exec(text("PRAGMA journal_mode")).one()
    assert str(mode[0]).lower() == "wal"


def test_columns_present_after_migration(fresh_engine):
    # Migrations target these columns; confirm they exist regardless of whether
    # create_all or the ALTER path produced them (prod-shaped DB safety).
    with Session(fresh_engine) as s:
        dl_cols = {r[1] for r in s.exec(text("PRAGMA table_info(download)")).all()}
        lib_cols = {r[1] for r in s.exec(text("PRAGMA table_info(library)")).all()}
        wanted_cols = {r[1] for r in s.exec(text("PRAGMA table_info(wanted_games)")).all()}
    assert {"source_id", "ra_game_id"} <= dl_cols
    assert {"cover_path", "hashed_at"} <= lib_cols
    assert "last_hunt_at" in wanted_cols
