from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
from sqlmodel import SQLModel, Session

from app.db.database import engine
from app.db.models import AppSetting
from app.routers import games, downloads, library, settings_router


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
    # Seed default settings if not already present
    with Session(engine) as session:
        for key, value in DEFAULT_SETTINGS.items():
            if not session.get(AppSetting, key):
                session.add(AppSetting(key=key, value=value))
        session.commit()
    yield


app = FastAPI(title="ROM Finder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(games.router)
app.include_router(downloads.router)
app.include_router(library.router)
app.include_router(settings_router.router)
