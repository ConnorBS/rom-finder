"""Phase 0: tracked migration registry + SQLite pragmas."""

import re

from sqlmodel import Session, select, text

import app.db.migrations as migrations
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


def test_every_defined_migration_is_registered():
    """Regression for the /goals 500: `_m_0021_goal_points` was defined but never
    appended to MIGRATIONS, so a pre-existing `goal` table never got the `points`
    column. Every `_m_XXXX_*` function must be registered."""
    defined = {n for n in dir(migrations) if re.fullmatch(r"_m_\d{4}_.+", n)}
    registered = {fn.__name__ for _vid, fn in MIGRATIONS}
    missing = defined - registered
    assert not missing, f"migration functions defined but not registered: {sorted(missing)}"


def test_old_goal_table_gets_points_backfilled(fresh_engine):
    """Mirror the actual prod shape that 500'd /goals: a `goal` table with every
    column EXCEPT `points` (created pre-0021), then run migrations and confirm
    0021 ALTER-adds it and the ORM can read a pre-existing row (the /goals path)."""
    from app.db.database import engine
    from app.db.models import Goal

    with Session(engine) as s:
        s.exec(text("DROP TABLE IF EXISTS goal"))
        # All model columns EXCEPT `points` (the one that was never registered).
        s.exec(text(
            "CREATE TABLE goal ("
            "id INTEGER PRIMARY KEY, game_title VARCHAR NOT NULL, system VARCHAR DEFAULT '', "
            "ra_game_id INTEGER, achievement_id INTEGER, cover_path VARCHAR DEFAULT '', "
            "objective VARCHAR DEFAULT 'beaten', custom_text VARCHAR DEFAULT '', "
            "achievement_desc VARCHAR DEFAULT '', event_name VARCHAR DEFAULT '', "
            "deadline TIMESTAMP, status VARCHAR DEFAULT 'active', auto BOOLEAN DEFAULT 0, "
            "created_at TIMESTAMP, updated_at TIMESTAMP, completed_at TIMESTAMP)"
        ))
        s.exec(text("INSERT INTO goal (game_title) VALUES ('Old Goal')"))
        # This minimal table also predates the later goal-column migrations, so un-register
        # them too and let run_migrations() ALTER-add every missing column.
        s.exec(text("DELETE FROM schema_migrations WHERE version IN "
                    "('0021_goal_points', '0026_goal_custom_display')"))
        s.commit()

    run_migrations()

    with Session(engine) as s:
        cols = {r[1] for r in s.exec(text("PRAGMA table_info(goal)")).all()}
        assert "points" in cols
        g = s.exec(select(Goal)).first()   # the exact query /goals runs
        assert g.game_title == "Old Goal" and g.points == 0
