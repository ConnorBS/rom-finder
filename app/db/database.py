from sqlmodel import create_engine, Session
from sqlalchemy import event
from app.config import get_settings

settings = get_settings()
engine = create_engine(settings.db_url, echo=settings.debug)


# WAL + busy_timeout let the scheduler, live downloads, and bulk verify write
# concurrently without "database is locked". WAL is persistent once set; the
# others are per-connection so must be applied on every connect.
if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def get_session():
    with Session(engine) as session:
        yield session
