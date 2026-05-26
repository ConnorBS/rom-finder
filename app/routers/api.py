"""JSON API for the Chrome extension (and any other external clients)."""

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session, select
from sqlalchemy import func, select as sa_select
from typing import Optional

from app.db.database import get_session
from app.db.models import (
    AppSetting, WantedGame, HuntStatus,
    Download, DownloadStatus, LibraryEntry, AppLog, InstalledExtension,
)
from app.services import sources as source_registry
from app.services.cover_sources import registry as cover_source_registry
from app.services.ra_client import SYSTEMS
from app.services.title_utils import clean_title

router = APIRouter(prefix="/api")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _count(session: Session, model, *conditions) -> int:
    stmt = sa_select(func.count()).select_from(model)
    for c in conditions:
        stmt = stmt.where(c)
    return session.scalar(stmt) or 0


def _enabled_source_ids(session: Session) -> set[str]:
    enabled = set()
    for src in source_registry.all_sources():
        key = f"source_{src.source_id}_enabled"
        if _get_setting(session, key, "false") == "true":
            enabled.add(src.source_id)
    return enabled


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@router.get("/ping")
async def ping():
    return {"status": "ok"}


@router.get("/diag/hash-lookup")
async def diag_hash_lookup(q: str = "Pokemon Blue Version", system_id: int = 4, game_id: int = 586, session: Session = Depends(get_session)):
    """Diagnostic: search RA for a game, fetch its known hashes, test API_GetGameInfoByMD5.

    Confirms whether the lookup endpoint returns a match for hashes RA
    definitively has in its database (ruling out credential or endpoint issues).
    """
    from app.services.ra_client import RAClient
    ra_username = _get_setting(session, "ra_username")
    ra_api_key = _get_setting(session, "ra_api_key")
    if not ra_username or not ra_api_key:
        return {"error": "no credentials"}
    ra = RAClient(ra_username, ra_api_key)

    # Search for the game to get its RA game ID
    try:
        games = await ra.search_games(system_id, q)
    except Exception as e:
        return {"error": f"search_games failed: {e}"}
    if not games:
        return {"error": f"no games found for '{q}' on system {system_id}"}

    game = games[0]
    game_id = game.get("ID") or game.get("GameID")
    game_title = game.get("Title", "?")

    # Fetch known hashes for this game
    try:
        hashes = await ra.get_game_hashes_full(game_id)
    except Exception as e:
        return {"error": f"get_game_hashes failed for game {game_id}: {e}"}
    if not hashes:
        return {"error": f"no hashes for game {game_id} ({game_title})", "game_id": game_id}

    # Test the first known hash against API_GetGameInfoByMD5
    test_hash = hashes[0]["MD5"]
    try:
        match = await ra.lookup_hash(test_hash)
    except Exception as e:
        return {"error": f"lookup_hash raised: {e}", "test_hash": test_hash}

    return {
        "game_found": game_title,
        "game_id": game_id,
        "known_hashes": [{"md5": h["MD5"], "name": h.get("Name", "")} for h in hashes[:5]],
        "test_hash": test_hash,
        "lookup_result": match,
        "verdict": "API_GetGameInfoByMD5 WORKS ✓" if match else "API_GetGameInfoByMD5 RETURNS NO MATCH ✗",
    }


# ---------------------------------------------------------------------------
# Wanted
# ---------------------------------------------------------------------------

class WantedAddRequest(BaseModel):
    ra_game_id: int
    game_title: str
    system: str = ""
    system_id: Optional[int] = None


@router.post("/wanted")
async def api_add_wanted(
    req: WantedAddRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    existing = session.exec(
        select(WantedGame).where(WantedGame.ra_game_id == req.ra_game_id)
    ).first()
    if existing:
        return {"status": "exists", "id": existing.id, "game_title": existing.game_title}

    # Use canonical system name from SYSTEMS dict when system_id is provided
    system = SYSTEMS.get(req.system_id, req.system) if req.system_id else req.system
    game = WantedGame(
        game_title=clean_title(req.game_title),
        system=system,
        ra_game_id=req.ra_game_id,
    )
    session.add(game)
    session.commit()
    session.refresh(game)

    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if username and api_key:
        from app.routers.wanted import _fetch_cover
        background_tasks.add_task(_fetch_cover, game.id, req.ra_game_id, username, api_key)

    return {"status": "added", "id": game.id, "game_title": game.game_title}


@router.get("/wanted")
async def api_list_wanted(session: Session = Depends(get_session)):
    games = session.exec(select(WantedGame)).all()
    return [
        {
            "id": g.id,
            "ra_game_id": g.ra_game_id,
            "game_title": g.game_title,
            "system": g.system,
            "status": g.status,
        }
        for g in games
    ]


# ---------------------------------------------------------------------------
# Source search
# ---------------------------------------------------------------------------

@router.get("/search")
async def api_search(
    q: str,
    system: str = "",
    session: Session = Depends(get_session),
):
    if not q:
        return []

    enabled_ids = _enabled_source_ids(session)
    results = []
    for src in source_registry.enabled_sources(enabled_ids):
        try:
            items = await src.search(q, system)
            for item in items:
                item["_source_name"] = src.name
            results.extend(items)
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Agent-observable diagnostics (Phase 0.5)
#
# /api/status and /api/logs let any agent confirm the running app's state over
# HTTP — no browser, no Docker socket, no human checking the site. Each section
# is independently guarded so one failure can't blank the whole report.
# ---------------------------------------------------------------------------

@router.get("/status")
async def api_status(session: Session = Depends(get_session)):
    status: dict = {"app": "rom-finder", "version": os.environ.get("APP_VERSION", "dev")}

    try:
        from app.services.rahasher import rahasher_status
        status["rahasher"] = rahasher_status()
    except Exception as e:
        status["rahasher"] = {"error": str(e)}

    try:
        active = [
            DownloadStatus.pending, DownloadStatus.downloading,
            DownloadStatus.hashing, DownloadStatus.verifying,
        ]
        status["db"] = {
            "library_total": _count(session, LibraryEntry),
            "library_ra_matched": _count(session, LibraryEntry, LibraryEntry.ra_matched == True),  # noqa: E712
            "no_ra": _count(session, LibraryEntry, LibraryEntry.file_hash != None, LibraryEntry.ra_matched == False),  # noqa: E711,E712
            "library_unhashed": _count(session, LibraryEntry, LibraryEntry.file_hash == None),  # noqa: E711
            "wanted_total": _count(session, WantedGame),
            "wanted_verified": _count(session, WantedGame, WantedGame.status == HuntStatus.verified),
            "wanted_hunting": _count(session, WantedGame, WantedGame.status == HuntStatus.hunting),
            "downloads_active": _count(session, Download, Download.status.in_(active)),
            "downloads_pending_approval": _count(session, Download, Download.status == DownloadStatus.pending_approval),
            "downloads_failed": _count(session, Download, Download.status == DownloadStatus.failed),
        }
    except Exception as e:
        status["db"] = {"error": str(e)}

    try:
        sched = {}
        for task in ("scan", "hash", "autodiscover"):
            sched[task] = {
                "enabled": _get_setting(session, f"sched_{task}_enabled", "false") == "true",
                "time": _get_setting(session, f"sched_{task}_time", ""),
                "last_run": _get_setting(session, f"sched_{task}_last_run", ""),
            }
        status["scheduler"] = sched
    except Exception as e:
        status["scheduler"] = {"error": str(e)}

    # Resumable verify state (keys land in Phase 5; default gracefully until then).
    status["verify"] = {
        "in_progress": _get_setting(session, "ra_verify_in_progress", "false") == "true",
        "paused_until": _get_setting(session, "ra_verify_paused_until", ""),
        "last_run": _get_setting(session, "ra_verify_last_run", ""),
    }

    try:
        srcs = []
        for src in source_registry.all_sources():
            srcs.append({
                "id": src.source_id,
                "name": getattr(src, "name", src.source_id),
                "enabled": _get_setting(session, f"source_{src.source_id}_enabled", "false") == "true",
                "available": getattr(src, "available", True),
            })
        status["sources"] = srcs
    except Exception as e:
        status["sources"] = {"error": str(e)}

    try:
        loaded_ids = {s.source_id for s in source_registry.all_sources()}
        loaded_ids |= {c.source_id for c in cover_source_registry.all_sources()}
        exts = []
        for ext in session.exec(select(InstalledExtension)).all():
            exts.append({
                "id": ext.ext_id, "name": ext.name, "type": ext.ext_type,
                "version": ext.version, "enabled": ext.enabled,
                "loaded": ext.ext_id in loaded_ids,
            })
        status["extensions"] = exts
    except Exception as e:
        status["extensions"] = {"error": str(e)}

    try:
        hours = int(_get_setting(session, "diagnostics_recent_hours", "24") or "24")
        since = datetime.utcnow() - timedelta(hours=hours)
        levels = ["error", "warning"]
        count = _count(session, AppLog, AppLog.level.in_(levels), AppLog.ts >= since)
        latest = session.exec(
            select(AppLog)
            .where(AppLog.level.in_(levels), AppLog.ts >= since)
            .order_by(AppLog.ts.desc())
            .limit(10)
        ).all()
        status["recent_errors"] = {
            "window_hours": hours,
            "count": count,
            "latest": [
                {"ts": l.ts.isoformat(), "level": l.level, "category": l.category, "message": l.message}
                for l in latest
            ],
        }
    except Exception as e:
        status["recent_errors"] = {"error": str(e)}

    return status


@router.get("/logs")
async def api_logs(
    level: str = Query(default=""),
    category: str = Query(default=""),
    since: str = Query(default="", description="ISO-8601 UTC timestamp; only logs at/after this time"),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    """Machine-readable twin of the /logs page — lets an agent pull exactly the
    errors a change should have fixed or avoided."""
    import json
    q = select(AppLog).order_by(AppLog.ts.desc())
    if level:
        q = q.where(AppLog.level == level)
    if category:
        q = q.where(AppLog.category == category)
    if since:
        try:
            q = q.where(AppLog.ts >= datetime.fromisoformat(since))
        except ValueError:
            pass
    logs = session.exec(q.limit(limit)).all()
    out = []
    for l in logs:
        try:
            details = json.loads(l.details or "{}")
        except Exception:
            details = {}
        out.append({
            "id": l.id, "ts": l.ts.isoformat(), "level": l.level,
            "category": l.category, "message": l.message, "details": details,
        })
    return out
