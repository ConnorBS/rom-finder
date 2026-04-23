from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.db.database import get_session
from app.db.models import AppSetting
from app.services import logger as applog

router = APIRouter(prefix="/scheduler")
templates = Jinja2Templates(directory="app/templates")


def _get(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _set(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key) or AppSetting(key=key)
    s.value = value
    session.add(s)


def _task_list(session: Session) -> list[dict]:
    return [
        {
            "id": "scan",
            "name": "Library scan",
            "description": "Walk the ROMs directory and import any files that aren't tracked in the library yet.",
            "enabled": _get(session, "sched_scan_enabled", "true"),
            "time": _get(session, "sched_scan_time", "04:00"),
            "last_run": _get(session, "sched_scan_last_run", ""),
        },
        {
            "id": "hash",
            "name": "Hash check",
            "description": "Hash all un-hashed ROMs. If a file has changed since it was last hashed, the old hash is cleared and the file is re-hashed.",
            "enabled": _get(session, "sched_hash_enabled", "true"),
            "time": _get(session, "sched_hash_time", "04:00"),
            "last_run": _get(session, "sched_hash_last_run", ""),
        },
        {
            "id": "autodiscover",
            "name": "RA autodiscover",
            "description": "Check RetroAchievements for newly-added achievement sets in your tracked systems and add missing games to the Wanted pool.",
            "enabled": _get(session, "sched_autodiscover_enabled", "true"),
            "time": _get(session, "sched_autodiscover_time", "04:00"),
            "last_run": _get(session, "sched_autodiscover_last_run", ""),
        },
    ]


@router.get("", response_class=HTMLResponse)
async def scheduler_page(request: Request, session: Session = Depends(get_session)):
    applog.log_navigation("scheduler")
    return templates.TemplateResponse(
        request, "scheduler.html", {"tasks": _task_list(session)}
    )


@router.post("/save", response_class=HTMLResponse)
async def save_schedule(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for tid in ("scan", "hash", "autodiscover"):
        _set(session, f"sched_{tid}_enabled", "true" if form.get(f"sched_{tid}_enabled") == "true" else "false")
        time_val = str(form.get(f"sched_{tid}_time", "04:00")).strip() or "04:00"
        _set(session, f"sched_{tid}_time", time_val)
    session.commit()
    applog.log_settings("Scheduler saved", {})
    return HTMLResponse('<span class="text-green-400 text-xs">&#10003; Schedule saved.</span>')


@router.post("/run/{task_id}", response_class=HTMLResponse)
async def run_task_now(task_id: str):
    from app.services.scheduler import run_scan, run_hash_check, run_autodiscover
    runners = {"scan": run_scan, "hash": run_hash_check, "autodiscover": run_autodiscover}
    fn = runners.get(task_id)
    if not fn:
        return HTMLResponse('<span class="text-red-400 text-xs">Unknown task.</span>')
    try:
        result = await fn()
        if "error" in result:
            return HTMLResponse(f'<span class="text-red-400 text-xs">&#10007; {result["error"]}</span>')
        parts = []
        if result.get("added"):     parts.append(f"{result['added']} added")
        if result.get("cleared"):   parts.append(f"{result['cleared']} stale cleared")
        if result.get("hashed"):    parts.append(f"{result['hashed']} hashed")
        if result.get("systems_checked") is not None:
            parts.append(f"{result['systems_checked']} systems checked")
        summary = ", ".join(parts) if parts else "nothing to do"
        return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {summary}.</span>')
    except Exception as exc:
        return HTMLResponse(f'<span class="text-red-400 text-xs">&#10007; {exc}</span>')
