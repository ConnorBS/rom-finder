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


@router.get("/tray-mobile", response_class=HTMLResponse)
async def activity_tray_mobile(session: Session = Depends(get_session)):
    """Compact activity indicator for the mobile top bar.

    Returns a spinner + label when anything is active, empty string when idle.
    """
    downloads = session.exec(
        select(Download).where(Download.status.in_([s.value for s in _ACTIVE_STATUSES]))
    ).all()
    tasks = activity_store.get_active()
    total_active = len(downloads) + len(tasks)
    if total_active == 0:
        return HTMLResponse("")
    # Pick the most descriptive label from running tasks
    label = next((t.label for t in tasks if t.label), None)
    if not label and downloads:
        label = f"Downloading {downloads[0].game_title}"
    if not label:
        label = "Working…"
    return HTMLResponse(
        f'<span class="flex items-center gap-1.5 text-xs text-blue-400">'
        f'<svg class="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">'
        f'<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>'
        f'<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>'
        f'</svg>'
        f'<span class="truncate max-w-[140px]">{label}</span>'
        f'</span>'
    )


@router.get("/card-states")
async def card_states():
    return JSONResponse(activity_store.get_card_states())


@router.post("/cancel/{task_id}", response_class=HTMLResponse)  # router prefix adds /activity
async def cancel_task(task_id: str):
    activity_store.cancel(task_id)
    return HTMLResponse("")
