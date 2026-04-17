"""Activity log viewer."""

import json
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select, delete

from app.db.database import get_session
from app.db.models import AppLog

router = APIRouter(prefix="/logs")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    level: str = Query(default=""),
    category: str = Query(default=""),
    session: Session = Depends(get_session),
):
    q = select(AppLog).order_by(AppLog.ts.desc())
    if level:
        q = q.where(AppLog.level == level)
    if category:
        q = q.where(AppLog.category == category)
    logs = session.exec(q.limit(500)).all()

    # Decode details JSON for template rendering
    entries = []
    for log in logs:
        try:
            details = json.loads(log.details or "{}")
        except Exception:
            details = {}
        entries.append({"log": log, "details": details})

    return templates.TemplateResponse(
        request, "logs.html",
        {"entries": entries, "level_filter": level, "category_filter": category},
    )


@router.post("/clear", response_class=HTMLResponse)
async def clear_logs(session: Session = Depends(get_session)):
    session.exec(delete(AppLog))
    session.commit()
    return HTMLResponse('<p class="text-green-400 text-sm">Logs cleared.</p>')


@router.get("/download", response_class=PlainTextResponse)
async def download_logs(
    level: str = Query(default=""),
    category: str = Query(default=""),
    session: Session = Depends(get_session),
):
    q = select(AppLog).order_by(AppLog.ts.asc())
    if level:
        q = q.where(AppLog.level == level)
    if category:
        q = q.where(AppLog.category == category)
    logs = session.exec(q.limit(5000)).all()

    lines = []
    for log in logs:
        try:
            details = json.loads(log.details or "{}")
            detail_str = "  " + json.dumps(details, indent=2).replace("\n", "\n  ") if details else ""
        except Exception:
            detail_str = ""
        ts = log.ts.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts} [{log.level.upper():7}] [{log.category:10}] {log.message}")
        if detail_str:
            lines.append(detail_str)

    return "\n".join(lines)
