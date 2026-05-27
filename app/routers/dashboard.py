"""RetroAchievements dashboard — pages driven by the local mirror (ra_dashboard.py).

All reads are local (zero RA calls). The only RA-touching action is the manual
Refresh, which runs ra_dashboard.refresh() as a BackgroundTask and reports progress
via the activity tray.
"""
from datetime import datetime

from fastapi import APIRouter, Request, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import RAGameProgress
from app.services import ra_dashboard
from app.services import ra_report
from app.services import settings as app_settings

router = APIRouter(prefix="/dashboard")
templates = Jinja2Templates(directory="app/templates")


def _parse_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _ra_configured(session: Session) -> bool:
    return bool(app_settings.get(session, "ra_username") and app_settings.get(session, "ra_api_key"))


@router.get("", response_class=HTMLResponse)
async def overview_page(request: Request, session: Session = Depends(get_session)):
    data = ra_dashboard.overview(session)
    return templates.TemplateResponse(request, "dashboard/overview.html", {
        **data,
        "ra_configured": _ra_configured(session),
    })


@router.post("/refresh", response_class=HTMLResponse)
async def refresh(background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
    if not _ra_configured(session):
        return HTMLResponse(
            '<span class="text-yellow-400 text-xs">Add RetroAchievements credentials in Settings first.</span>'
        )
    background_tasks.add_task(ra_dashboard.refresh)
    return HTMLResponse(
        '<span class="text-blue-400 text-xs">&#8635; Syncing from RetroAchievements… '
        'watch the activity tray, then reload when it finishes.</span>'
    )


@router.get("/timeline", response_class=HTMLResponse)
async def timeline_page(
    request: Request,
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    q: str = Query(default=""),
    console: str = Query(default=""),
    hardcore: str = Query(default=""),     # "", "hardcore", "softcore"
    session: Session = Depends(get_session),
):
    hc = True if hardcore == "hardcore" else (False if hardcore == "softcore" else None)
    data = ra_dashboard.timeline(session, _parse_date(date_from), _parse_date(date_to), q, console, hc)
    return templates.TemplateResponse(request, "dashboard/timeline.html", {
        **data,
        "ra_configured": _ra_configured(session),
        "f_date_from": date_from, "f_date_to": date_to, "f_q": q,
        "f_console": console, "f_hardcore": hardcore,
    })


@router.get("/games", response_class=HTMLResponse)
async def games_page(
    request: Request,
    console: str = Query(default=""),
    q: str = Query(default=""),
    owned: str = Query(default=""),        # "1" = owned only
    award: str = Query(default=""),        # "mastered"
    sort: str = Query(default="recent"),   # recent|completion|achievements|title
    session: Session = Depends(get_session),
):
    data = ra_dashboard.games(session, console, q, owned == "1", award, sort)
    return templates.TemplateResponse(request, "dashboard/games.html", {
        **data,
        "ra_configured": _ra_configured(session),
        "f_console": console, "f_q": q, "f_owned": owned, "f_award": award, "f_sort": sort,
    })


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, session: Session = Depends(get_session)):
    data = ra_dashboard.insights(session)
    return templates.TemplateResponse(request, "dashboard/insights.html", {
        **data,
        "ra_configured": _ra_configured(session),
    })


# --- Reports (RetroAchievements forum markup) ------------------------------

def _build_report(session, report_type, date_from, date_to, game_id, q, console, hardcore):
    hc = True if hardcore == "hardcore" else (False if hardcore == "softcore" else None)
    return ra_report.build(
        session, report_type,
        date_from=_parse_date(date_from), date_to=_parse_date(date_to),
        game_id=game_id or 0, q=q, console=console, hardcore=hc,
    )


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, session: Session = Depends(get_session)):
    games = session.exec(select(RAGameProgress).order_by(RAGameProgress.title)).all()
    return templates.TemplateResponse(request, "dashboard/reports.html", {
        "games": games,
        "consoles": ra_dashboard._console_names(session),
        "ra_configured": _ra_configured(session),
    })


@router.get("/reports/preview", response_class=HTMLResponse)
async def reports_preview(
    request: Request,
    report_type: str = Query(default="recap"),
    date_from: str = Query(default=""), date_to: str = Query(default=""),
    game_id: int = Query(default=0), q: str = Query(default=""),
    console: str = Query(default=""), hardcore: str = Query(default=""),
    session: Session = Depends(get_session),
):
    markup = _build_report(session, report_type, date_from, date_to, game_id, q, console, hardcore)
    from urllib.parse import urlencode
    download_qs = urlencode({
        "report_type": report_type, "date_from": date_from, "date_to": date_to,
        "game_id": game_id, "q": q, "console": console, "hardcore": hardcore,
    })
    return templates.TemplateResponse(request, "dashboard/_report_output.html",
                                      {"markup": markup, "download_qs": download_qs})


@router.get("/reports/download")
async def reports_download(
    report_type: str = Query(default="recap"),
    date_from: str = Query(default=""), date_to: str = Query(default=""),
    game_id: int = Query(default=0), q: str = Query(default=""),
    console: str = Query(default=""), hardcore: str = Query(default=""),
    session: Session = Depends(get_session),
):
    markup = _build_report(session, report_type, date_from, date_to, game_id, q, console, hardcore)
    return StreamingResponse(
        iter([markup]), media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=ra-{report_type}-report.md"},
    )
