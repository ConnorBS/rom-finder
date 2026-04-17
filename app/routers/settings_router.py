from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from pathlib import Path

from app.db.database import get_session
from app.db.models import AppSetting
from app.services import sources as source_registry

router = APIRouter(prefix="/settings")
templates = Jinja2Templates(directory="app/templates")


def get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def set_setting(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key) or AppSetting(key=key)
    s.value = value
    session.add(s)


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, session: Session = Depends(get_session)):
    current = {
        "download_dir": get_setting(session, "download_dir", str(Path.home() / "ROMs")),
        "ra_enabled": get_setting(session, "ra_enabled", "false"),
        "ra_username": get_setting(session, "ra_username"),
        "ra_api_key": get_setting(session, "ra_api_key"),
    }
    all_srcs = source_registry.all_sources()
    src_enabled = {
        src.source_id: get_setting(session, f"source_{src.source_id}_enabled", "false") == "true"
        for src in all_srcs
    }
    return templates.TemplateResponse(
        request, "settings.html",
        {"settings": current, "sources": all_srcs, "source_enabled": src_enabled},
    )


@router.post("", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    session: Session = Depends(get_session),
    download_dir: str = Form(...),
    ra_enabled: str = Form(default="false"),
    ra_username: str = Form(default=""),
    ra_api_key: str = Form(default=""),
):
    set_setting(session, "download_dir", download_dir)
    set_setting(session, "ra_enabled", ra_enabled)
    set_setting(session, "ra_username", ra_username)
    set_setting(session, "ra_api_key", ra_api_key)

    # Source toggles — checkbox values are only present when checked
    form_data = await request.form()
    for src in source_registry.all_sources():
        key = f"source_{src.source_id}_enabled"
        value = "true" if form_data.get(key) == "true" else "false"
        set_setting(session, key, value)

    session.commit()

    return HTMLResponse(
        '<div id="settings-toast" class="bg-green-900/50 border border-green-700 '
        'text-green-300 px-4 py-3 rounded-lg text-sm">'
        'Settings saved.</div>'
    )
