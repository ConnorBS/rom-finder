import asyncio
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
from sqlmodel import SQLModel, Session, text

from app.db.database import engine
from app.db.models import AppSetting, WantedGame, AppLog, HuntAttempt, InstalledExtension  # noqa: F401 — registers tables
from app.routers import games, downloads, library, settings_router, wanted, api, logs, collection, activity, scheduler, extensions as extensions_router


# (table, column, sql_type, default_expr or None for nullable)
_MIGRATIONS = [
    ("download", "source_id", "VARCHAR", "'archive_org'"),
    ("download", "ra_game_id", "INTEGER", None),
    ("library", "cover_path", "VARCHAR", "''"),
    ("library", "hashed_at", "TIMESTAMP", None),
    ("wanted_games", "last_hunt_at", "TIMESTAMP", None),
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
    # Source enabled flags — archive.org built-in; others are extensions
    "source_archive_org_enabled": "true",
    # Extension system
    "extensions_dir": os.environ.get("EXTENSIONS_DIR", "extensions"),
    "extension_repos": '["https://raw.githubusercontent.com/ConnorBS/rom-finder/main/extensions/index.json"]',
    # Verbose logging captures every page load, button press, and navigation event
    "verbose_logging": "false",
    # When true, downloads stage in check_dir for manual review before moving to download_dir.
    # When false, downloads go directly to download_dir and are auto-imported.
    "use_review_dir": "true",
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
    # Scheduler — daily task schedule (local time HH:MM)
    "sched_scan_enabled": "true",
    "sched_scan_time": "04:00",
    "sched_scan_last_run": "",
    "sched_hash_enabled": "true",
    "sched_hash_time": "04:00",
    "sched_hash_last_run": "",
    "sched_autodiscover_enabled": "true",
    "sched_autodiscover_time": "04:00",
    "sched_autodiscover_last_run": "",
}


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
        # Load installed extensions
        ext_setting = session.get(AppSetting, "extensions_dir")
        ext_dir = ext_setting.value if ext_setting else "extensions"
    Path(ext_dir).mkdir(parents=True, exist_ok=True)
    from app.services.extension_loader import load_all_extensions
    load_all_extensions(ext_dir)
    from app.services import logger as applog
    applog.info("system", "ROM Finder started")
    from app.services.scheduler import scheduler_loop
    sched_task = asyncio.create_task(scheduler_loop())
    yield
    sched_task.cancel()


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
app.include_router(scheduler.router)
app.include_router(extensions_router.router)
