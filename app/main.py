from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
from sqlmodel import SQLModel, Session, text

from app.db.database import engine
from app.db.models import AppSetting, WantedGame  # noqa: F401 — registers table
from app.routers import games, downloads, library, settings_router, wanted, api


# (column_name, sql_type, default_value)
_MIGRATIONS = [
    ("download", "source_id", "VARCHAR", "'archive_org'"),
]


def _run_migrations() -> None:
    """Add any columns that exist in the model but not yet in the DB."""
    with Session(engine) as session:
        for table, col, col_type, default in _MIGRATIONS:
            rows = session.exec(text(f"PRAGMA table_info({table})")).all()
            existing = {r[1] for r in rows}
            if col not in existing:
                session.exec(
                    text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default}")
                )
        session.commit()


DEFAULT_SETTINGS = {
    "download_dir": str(Path.home() / "ROMs"),
    "ra_enabled": "false",
    "ra_username": "",
    "ra_api_key": "",
    # Source enabled flags — archive.org on by default, others opt-in
    "source_archive_org_enabled": "true",
    "source_vimm_enabled": "false",
    "source_romsfun_enabled": "false",
    "source_wowroms_enabled": "false",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    _run_migrations()
    Path("static/covers").mkdir(parents=True, exist_ok=True)
    # Seed default settings if not already present
    with Session(engine) as session:
        for key, value in DEFAULT_SETTINGS.items():
            if not session.get(AppSetting, key):
                session.add(AppSetting(key=key, value=value))
        session.commit()
    yield


app = FastAPI(title="ROM Finder", lifespan=lifespan)

# Allow requests from the Chrome extension (chrome-extension://*) and any
# local/remote address the user might host this on.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(games.router)
app.include_router(downloads.router)
app.include_router(library.router)
app.include_router(settings_router.router)
app.include_router(wanted.router)
app.include_router(api.router)
