import os
import tempfile

# Point the app at a throwaway DB *before* any app module imports the engine.
_TMP_DB = os.path.join(tempfile.gettempdir(), "romfinder_test.db")
os.environ["DB_URL"] = f"sqlite:///{_TMP_DB.replace(os.sep, '/')}"

import pytest  # noqa: E402


def _wipe_db_files() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = _TMP_DB + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


@pytest.fixture()
def fresh_engine():
    """A clean SQLite DB with schema + migrations applied (WAL pragmas active)."""
    _wipe_db_files()
    from sqlmodel import SQLModel
    import app.db.models  # noqa: F401 — registers tables on SQLModel.metadata
    from app.db.database import engine
    from app.db.migrations import run_migrations

    SQLModel.metadata.create_all(engine)
    run_migrations()
    yield engine
    engine.dispose()
    _wipe_db_files()


@pytest.fixture()
def client():
    """A TestClient with the full app lifespan run (migrations + settings seed +
    extension load + RAHasher check) against the throwaway DB."""
    import app.db.models  # noqa: F401
    from app.db.database import engine
    engine.dispose()      # release WAL connections so the wipe actually deletes
    _wipe_db_files()
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c
    engine.dispose()
    _wipe_db_files()
