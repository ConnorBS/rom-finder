from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import Download, DownloadStatus
from app.services import activity as activity_store

router = APIRouter(prefix="/activity")
templates = Jinja2Templates(directory="app/templates")

_ACTIVE_STATUSES = {DownloadStatus.pending, DownloadStatus.downloading, DownloadStatus.hashing}


@router.get("/tray", response_class=HTMLResponse)
async def activity_tray(request: Request, session: Session = Depends(get_session)):
    downloads = session.exec(
        select(Download).where(Download.status.in_([s.value for s in _ACTIVE_STATUSES]))
    ).all()
    tasks = activity_store.get_active()
    return templates.TemplateResponse(
        request, "partials/activity_tray.html",
        {"downloads": downloads, "tasks": tasks},
    )
