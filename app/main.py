import asyncio
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from sqlmodel import SQLModel, Session, text

from app.db.database import engine
from app.db.models import AppSetting, WantedGame, AppLog  # noqa: F401 — registers tables
from app.routers import games, downloads, library, settings_router, wanted, api, logs, collection, activity


# (table, column, sql_type, default_expr or None for nullable)
_MIGRATIONS = [
    ("download", "source_id", "VARCHAR", "'archive_org'"),
    ("download", "ra_game_id", "INTEGER", None),
    ("library", "cover_path", "VARCHAR", "''"),
]


def _run_migrations() -> None:
    """Add any columns that exist in the model but not yet in the DB."""
    with Session(engine) as session:
        for table, col, col_type, default in _MIGRATIONS:
            rows = session.exec(text(f"PRAGMA table_info({table})")).all()
            existing = {r[1] for r in rows}
            if col not in existing:
                if default is not None:
                    session.exec(
                        text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default}")
                    )
                else:
                    session.exec(
                        text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                    )
        session.commit()


DEFAULT_SETTINGS = {
    "download_dir": os.environ.get("DOWNLOAD_DIR", str(Path.home() / "ROMs")),
    "check_dir": os.environ.get("CHECK_DIR", str(Path.home() / "ROMs-check")),
    "covers_dir": os.environ.get("COVERS_DIR", "static/covers"),
    "folder_map": "{}",
    "ra_enabled": "false",
    "ra_username": "",
    "ra_api_key": "",
    # Source enabled flags — archive.org on by default, others opt-in
    "source_archive_org_enabled": "true",
    "source_vimm_enabled": "false",
    "source_romsfun_enabled": "false",
    "source_wowroms_enabled": "false",
    # Verbose logging captures every page load, button press, and navigation event
    "verbose_logging": "false",
    # Per-directory read-only locks — prevent any edits, deletes, or writes within the app
    "download_dir_readonly": "false",
    "check_dir_readonly": "false",
    "covers_dir_readonly": "false",
    # Cover art sources — RA on by default, SteamGridDB opt-in
    "cover_sources_order": '["retroachievements", "steamgriddb"]',
    "cover_source_retroachievements_enabled": "true",
    "cover_source_steamgriddb_enabled": "false",
    "cover_source_steamgriddb_api_key": "",
    # Autodiscover — periodically add newly-released RA games to Wanted pool
    "ra_autodiscover_enabled": "false",
    "ra_autodiscover_last_checked": "",
}


async def _autodiscover_loop() -> None:
    """Wake up every hour; run autodiscover when enabled and the interval has elapsed."""
    while True:
        await asyncio.sleep(3600)
        with Session(engine) as session:
            enabled = session.get(AppSetting, "ra_autodiscover_enabled")
            if not enabled or enabled.value != "true":
                continue
            last_setting = session.get(AppSetting, "ra_autodiscover_last_checked")
            last_str = last_setting.value if last_setting else ""

        should_run = False
        if not last_str:
            should_run = True
        else:
            try:
                elapsed = (datetime.utcnow() - datetime.fromisoformat(last_str)).total_seconds()
                if elapsed >= 86400:  # 24 hours
                    should_run = True
            except ValueError:
                should_run = True

        if should_run:
            try:
                from app.services.autodiscover import run_autodiscover
                await run_autodiscover()
            except Exception as exc:
                from app.services import logger as _log
                _log.warning("autodiscover", f"Loop error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    _run_migrations()
    # Seed default settings if not already present
    with Session(engine) as session:
        for key, value in DEFAULT_SETTINGS.items():
            if not session.get(AppSetting, key):
                session.add(AppSetting(key=key, value=value))
        session.commit()
        # Ensure the configured covers directory exists
        covers_setting = session.get(AppSetting, "covers_dir")
        covers_path = covers_setting.value if covers_setting else "static/covers"
        Path(covers_path).mkdir(parents=True, exist_ok=True)
    from app.services import logger as applog
    applog.info("system", "ROM Finder started")
    task = asyncio.create_task(_autodiscover_loop())
    yield
    task.cancel()


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
app.include_router(collection.router)
app.include_router(api.router)
app.include_router(logs.router)
app.include_router(activity.router)
