import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
from sqlmodel import SQLModel, Session

from app.db.database import engine
from app.db.migrations import run_migrations
from app.db.models import AppSetting, WantedGame, AppLog, HuntAttempt, InstalledExtension  # noqa: F401 — registers tables
from app.services import settings as app_settings
from app.services.settings import DEFAULT_SETTINGS
from app.routers import games, downloads, library, settings_router, wanted, api, logs, collection, activity, scheduler, extensions as extensions_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    run_migrations()  # includes 0010 normalize_system_names (the old WiiWii data-fix)
    # Seed default settings if not already present
    with Session(engine) as session:
        for key, value in DEFAULT_SETTINGS.items():
            if not session.get(AppSetting, key):
                session.add(AppSetting(key=key, value=value))
        session.commit()
        # Ensure the configured covers directory exists
        covers_path = app_settings.get(session, "covers_dir", "static/covers")
        Path(covers_path).mkdir(parents=True, exist_ok=True)
        # Load installed extensions
        ext_dir = app_settings.get(session, "extensions_dir", "extensions")
        # Gather which extensions are enabled and their settings
        from sqlmodel import select as _select
        installed_exts = session.exec(_select(InstalledExtension)).all()
        enabled_ext_ids = {e.ext_id for e in installed_exts if e.enabled}
        ext_configs: dict[str, dict] = {
            ext.ext_id: app_settings.get_extension_config(session, ext.ext_id)
            for ext in installed_exts if ext.enabled
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
