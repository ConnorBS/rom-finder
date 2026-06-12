from datetime import datetime
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
        {
            "id": "verify",
            "name": "RA re-verify",
            "description": "Re-check hashed ROMs that aren't yet RA-matched against RetroAchievements. Resumable and rate-limit-aware — pauses and resumes on a 429 instead of failing, and clears the no-RA-match backlog over successive runs.",
            "enabled": _get(session, "sched_verify_enabled", "true"),
            "time": _get(session, "sched_verify_time", "05:00"),
            "last_run": _get(session, "sched_verify_last_run", ""),
        },
        {
            "id": "eventsync",
            "name": "Event sync",
            "description": "Re-check each imported auto-sync RA event (AotW, random rolls, etc.) for newly-added achievements and import them as goals. One RA request per event.",
            "enabled": _get(session, "sched_eventsync_enabled", "true"),
            "time": _get(session, "sched_eventsync_time", "05:30"),
            "last_run": _get(session, "sched_eventsync_last_run", ""),
        },
        {
            "id": "chdcheck",
            "name": "CHD format check",
            "description": (
                "Flag CHDs whose container uses the Zstandard codec (cdzs/zstd) — RetroArch "
                "can read them but its RetroAchievements hasher can't, so the game earns no "
                "achievements. When chdman is available it also re-encodes them onto a "
                "RA-safe codec (disc data verified identical). "
                + ("Enabled." if _get(session, "chd_format_check_enabled", "false") == "true"
                   else "Turn on “CHD format check” in Settings first.")
            ),
            "enabled": _get(session, "sched_chdcheck_enabled", "false"),
            "time": _get(session, "sched_chdcheck_time", "04:30"),
            "last_run": _get(session, "sched_chdcheck_last_run", ""),
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
    for tid in ("scan", "hash", "autodiscover", "verify", "eventsync", "chdcheck"):
        _set(session, f"sched_{tid}_enabled", "true" if form.get(f"sched_{tid}_enabled") == "true" else "false")
        time_val = str(form.get(f"sched_{tid}_time", "04:00")).strip() or "04:00"
        _set(session, f"sched_{tid}_time", time_val)
    session.commit()
    applog.log_settings("Scheduler saved", {})
    return HTMLResponse('<span class="text-green-400 text-xs">&#10003; Schedule saved.</span>')


def _oob_last_run(task_id: str) -> str:
    """Return an HTMX OOB element that refreshes the 'Last run' display for a task."""
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    return (
        f'<div id="last-run-{task_id}" hx-swap-oob="true" class="text-xs text-gray-600">'
        f'Last run: <span class="text-gray-500">{now_str} UTC</span>'
        f'</div>'
    )


@router.post("/run/{task_id}", response_class=HTMLResponse)
async def run_task_now(task_id: str):
    from app.services.scheduler import run_scan, run_hash_check, run_autodiscover, run_verify, run_event_sync, run_chd_check
    runners = {"scan": run_scan, "hash": run_hash_check,
               "autodiscover": run_autodiscover, "verify": run_verify,
               "eventsync": run_event_sync, "chdcheck": run_chd_check}
    fn = runners.get(task_id)
    if not fn:
        return HTMLResponse('<span class="text-red-400 text-xs">Unknown task.</span>')
    try:
        result = await fn()
        oob = _oob_last_run(task_id)

        if "error" in result:
            return HTMLResponse(f'<span class="text-red-400 text-xs">&#10007; {result["error"]}</span>{oob}')

        if task_id == "scan":
            added = result.get("added", 0)
            if added == 0:
                msg = "Library up to date — no new ROMs found."
                return HTMLResponse(f'<span class="text-gray-400 text-xs">{msg}</span>{oob}')
            parts = [f"{added} new ROM{'s' if added != 1 else ''} imported"]
            if result.get("hashed"):    parts.append(f"{result['hashed']} hashed")
            if result.get("verified"):  parts.append(f"{result['verified']} RA matched")
            return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {", ".join(parts)}.</span>{oob}')

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
                return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {", ".join(parts)}.</span>{oob}')
            note = " (files not accessible)" if skipped else ""
            return HTMLResponse(f'<span class="text-gray-400 text-xs">All ROMs already hashed — nothing to do{note}.</span>{oob}')

        if task_id == "autodiscover":
            added   = result.get("added", 0)
            systems = result.get("systems_checked", 0)
            if added:
                return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {added} new game{"s" if added != 1 else ""} added from {systems} system{"s" if systems != 1 else ""}.</span>{oob}')
            return HTMLResponse(f'<span class="text-gray-400 text-xs">No new games found across {systems} system{"s" if systems != 1 else ""}.</span>{oob}')

        if task_id == "verify":
            status = result.get("status", "")
            if status == "no_credentials":
                return HTMLResponse(f'<span class="text-yellow-500 text-xs">Add RA credentials in Settings first.</span>{oob}')
            if status == "paused":
                return HTMLResponse(f'<span class="text-yellow-500 text-xs">Rate-limited — paused until {result.get("paused_until","")} (will resume).</span>{oob}')
            checked, mat, rem = result.get("checked", 0), result.get("matched", 0), result.get("remaining", 0)
            note = " (hit rate-limit, paused — resumes next run)" if status == "rate_limited" else ""
            if checked == 0 and rem == 0:
                return HTMLResponse(f'<span class="text-gray-400 text-xs">Nothing to verify — all hashed ROMs checked.</span>{oob}')
            return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; Checked {checked}, {mat} newly matched, {rem} still pending{note}.</span>{oob}')

        if task_id == "eventsync":
            events_n = result.get("events", 0)
            created = result.get("created", 0)
            if not events_n:
                return HTMLResponse(f'<span class="text-gray-400 text-xs">No auto-sync events to check.</span>{oob}')
            if created:
                return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {created} new achievement goal{"s" if created != 1 else ""} added across {events_n} event{"s" if events_n != 1 else ""}.</span>{oob}')
            return HTMLResponse(f'<span class="text-gray-400 text-xs">Checked {events_n} event{"s" if events_n != 1 else ""} — no new achievements.</span>{oob}')

        if task_id == "chdcheck":
            if result.get("status") == "disabled":
                return HTMLResponse('<span class="text-yellow-500 text-xs">Turn on “CHD format check” in Settings first.</span>')
            checked = result.get("checked", 0)
            flagged = result.get("flagged", 0)
            converted = result.get("converted", 0)
            failed = result.get("failed", 0)
            still = result.get("still_bad", 0)
            if checked == 0:
                return HTMLResponse(f'<span class="text-gray-400 text-xs">No CHDs found to check.</span>{oob}')
            if flagged == 0:
                return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {checked} CHDs checked — all on RA-safe codecs.</span>{oob}')
            parts = [f"{flagged} on Zstandard"]
            if converted: parts.append(f"{converted} re-encoded")
            if failed:    parts.append(f"{failed} failed")
            tail = ""
            if still and not result.get("converted_in_app"):
                tail = " — re-encode with the batch scripts (chdman unavailable in-app)" if not result.get("chdman_available") else f" — {still} still need fixing"
            elif still:
                tail = f" — {still} still need fixing (see logs)"
            cls = "text-green-400" if converted and not failed else ("text-yellow-500" if still else "text-green-400")
            return HTMLResponse(f'<span class="{cls} text-xs">&#10003; {checked} checked, {", ".join(parts)}{tail}.</span>{oob}')

        return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; Done.</span>{oob}')
    except Exception as exc:
        return HTMLResponse(f'<span class="text-red-400 text-xs">&#10007; {exc}</span>')
