from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import Download, DownloadStatus
from app.services import activity as activity_store

router = APIRouter(prefix="/activity")
templates = Jinja2Templates(directory="app/templates")

_ACTIVE_STATUSES = {DownloadStatus.pending, DownloadStatus.downloading, DownloadStatus.hashing, DownloadStatus.verifying}


@router.get("/tray", response_class=HTMLResponse)
async def activity_tray(request: Request, session: Session = Depends(get_session)):
    downloads = session.exec(
        select(Download).where(Download.status.in_([s.value for s in _ACTIVE_STATUSES]))
    ).all()
    tasks = activity_store.get_active()

    # Separate the cover-batch task from individual cover tasks and other batch tasks
    cover_batch = next((t for t in tasks if t.task_id == "cover-batch"), None)
    individual_cover = [t for t in tasks if t.task_id.startswith("cover-") and t.task_id != "cover-batch"]
    other_tasks = [t for t in tasks if not t.task_id.startswith("cover-")]

    return templates.TemplateResponse(
        request, "partials/activity_tray.html",
        {
            "downloads": downloads,
            "cover_batch": cover_batch,
            "individual_cover": individual_cover,
            "tasks": other_tasks,
        },
    )


@router.get("/card-states")
async def card_states():
    return JSONResponse(activity_store.get_card_states())
