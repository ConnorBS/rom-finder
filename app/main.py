import asyncio
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
from sqlmodel import SQLModel, Session, text

from app.db.database import engine
from app.db.migrations import run_migrations
from app.db.models import AppSetting, WantedGame, AppLog, HuntAttempt, InstalledExtension  # noqa: F401 — registers tables
from app.routers import games, downloads, library, settings_router, wanted, api, logs, collection, activity, scheduler, extensions as extensions_router


DEFAULT_SETTINGS = {
    "download_dir": os.environ.get("DOWNLOAD_DIR", str(Path.home() / "ROMs")),
    "check_dir": os.environ.get("CHECK_DIR", str(Path.home() / "ROMs-check")),
    "covers_dir": os.environ.get("COVERS_DIR", "static/covers"),
    "folder_map": "{}",
    "ra_enabled": "false",
    "ra_username": "",
    "ra_api_key": "",
    # Source enabled flags
    "source_archive_org_enabled": "true",
    # Extension system
    "extensions_dir": os.environ.get("EXTENSIONS_DIR", "extensions"),
    "extension_repos": '["https://raw.githubusercontent.com/ConnorBS/rom-finder/main/extensions/index.json"]',
    # Verbose logging captures every page load, button press, and navigation event
    "verbose_logging": "false",
    # /api/status recent_errors window
    "diagnostics_recent_hours": "24",
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
    run_migrations()
    # Fix corrupted system names left by the Chrome extension concatenation bug
    # TODO(Phase 8): removed once title_utils.canonical_system + migration 0009 land
    # and the extension stops posting scraped link text.
    with Session(engine) as session:
        for table in ("wanted_games", "library"):
            session.exec(text(f"UPDATE {table} SET system = 'Wii' WHERE system = 'WiiWii'"))
        session.commit()
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
        # Gather which extensions are enabled and their settings
        from sqlmodel import select as _select
        installed_exts = session.exec(_select(InstalledExtension)).all()
        enabled_ext_ids = {e.ext_id for e in installed_exts if e.enabled}
        all_settings_rows = session.exec(_select(AppSetting)).all()
        all_settings_dict = {s.key: s.value for s in all_settings_rows}
        ext_configs: dict[str, dict] = {}
        for ext in installed_exts:
            if ext.enabled:
                prefix = f"ext_{ext.ext_id}_"
                ext_configs[ext.ext_id] = {
                    k[len(prefix):]: v
                    for k, v in all_settings_dict.items()
                    if k.startswith(prefix)
                }
    Path(ext_dir).mkdir(parents=True, exist_ok=True)
    from app.services.extension_loader import load_all_extensions
    load_all_extensions(ext_dir, enabled_ids=enabled_ext_ids, configs=ext_configs)
    from app.services import logger as applog
    from app.services.rahasher import _rahasher_available, _RAHASHER_BIN
    import shutil
    rahasher_path = shutil.which(_RAHASHER_BIN)
    if rahasher_path:
        print(f"[startup] RAHasher available: {rahasher_path}", flush=True)
        applog.info("system", "RAHasher available", {"path": rahasher_path})
    else:
        print("[startup] WARNING: RAHasher not found — disc-based ROMs (Saturn, PS1/2, Dreamcast…) will hash incorrectly", flush=True)
        applog.warning("system", "RAHasher not found — disc-based ROMs (Saturn, PS1/2, Dreamcast, CHD) will hash incorrectly and never match RA")
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
