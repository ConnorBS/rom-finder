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


# (version_id, apply_fn) — applied in order, recorded once.
MIGRATIONS: list[tuple[str, "callable"]] = [
    ("0001_download_source_id", _m_0001),
    ("0002_download_ra_game_id", _m_0002),
    ("0003_library_cover_path", _m_0003),
    ("0004_library_hashed_at", _m_0004),
    ("0005_wanted_last_hunt_at", _m_0005),
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
