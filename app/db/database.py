from sqlmodel import create_engine, Session
from sqlalchemy import event
from app.config import get_settings

settings = get_settings()

# Pool sized for many concurrent searches/downloads. The rule that keeps it from
# starving (the QueuePool "connection timed out" 500s): a pooled connection is
# only ever held for actual DB work, NEVER across a network await. The search /
# hunt endpoints read what they need in a short `with Session(engine)` block,
# release it, then do their slow httpx calls (source search, RA lookup) with no
# connection checked out — so suspended request coroutines hold zero connections.
# pool_pre_ping discards a connection that went stale between checkouts.
engine = create_engine(
    settings.db_url,
    echo=settings.debug,
    pool_size=20,
    max_overflow=40,
    pool_timeout=30,
    pool_pre_ping=True,
)


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
