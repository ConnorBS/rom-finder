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
            "description": "Walk the ROMs directory and import any untracked files. Each new ROM is then hashed, cover art is fetched, and its hash is checked against RetroAchievements.",
            "enabled": _get(session, "sched_scan_enabled", "true"),
            "time": _get(session, "sched_scan_time", "04:00"),
            "last_run": _get(session, "sched_scan_last_run", ""),
        },
        {
            "id": "hash",
            "name": "Hash check",
            "description": "Hash all un-hashed ROMs. Backfills missing timestamps on existing hashes, then clears and re-hashes any file whose modification time is newer than when it was last hashed.",
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

        if task_id == "scan":
            added = result.get("added", 0)
            if added == 0:
                msg = "Library up to date — no new ROMs found."
                return HTMLResponse(f'<span class="text-gray-400 text-xs">{msg}</span>')
            parts = [f"{added} new ROM{'s' if added != 1 else ''} imported"]
            if result.get("hashed"):    parts.append(f"{result['hashed']} hashed")
            if result.get("verified"):  parts.append(f"{result['verified']} RA matched")
            return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {", ".join(parts)}.</span>')

        if task_id == "hash":
            backfilled = result.get("backfilled", 0)
            cleared    = result.get("cleared", 0)
            hashed     = result.get("hashed", 0)
            skipped    = result.get("skipped", 0)
            parts = []
            if hashed:     parts.append(f"{hashed} hashed")
            if cleared:    parts.append(f"{cleared} stale cleared")
            if backfilled: parts.append(f"{backfilled} timestamps backfilled")
            if skipped:    parts.append(f"{skipped} files not found")
            if parts:
                return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {", ".join(parts)}.</span>')
            note = " (files not accessible)" if skipped else ""
            return HTMLResponse(f'<span class="text-gray-400 text-xs">All ROMs already hashed — nothing to do{note}.</span>')

        if task_id == "autodiscover":
            added   = result.get("added", 0)
            systems = result.get("systems_checked", 0)
            if added:
                return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {added} new game{"s" if added != 1 else ""} added from {systems} system{"s" if systems != 1 else ""}.</span>')
            return HTMLResponse(f'<span class="text-gray-400 text-xs">No new games found across {systems} system{"s" if systems != 1 else ""}.</span>')

        return HTMLResponse('<span class="text-green-400 text-xs">&#10003; Done.</span>')
    except Exception as exc:
        return HTMLResponse(f'<span class="text-red-400 text-xs">&#10007; {exc}</span>')
