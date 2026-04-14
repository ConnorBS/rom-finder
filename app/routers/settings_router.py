from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from pathlib import Path

from app.db.database import get_session
from app.db.models import AppSetting

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
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "settings": current},
    )


@router.post("", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    download_dir: str = Form(...),
    ra_enabled: str = Form(default="false"),
    ra_username: str = Form(default=""),
    ra_api_key: str = Form(default=""),
    session: Session = Depends(get_session),
):
    set_setting(session, "download_dir", download_dir)
    set_setting(session, "ra_enabled", ra_enabled)
    set_setting(session, "ra_username", ra_username)
    set_setting(session, "ra_api_key", ra_api_key)
    session.commit()

    return HTMLResponse(
        '<div id="settings-toast" class="bg-green-900/50 border border-green-700 '
        'text-green-300 px-4 py-3 rounded-lg text-sm">'
        'Settings saved.</div>'
    )
