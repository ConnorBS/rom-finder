"""JSON API for the Chrome extension (and any other external clients)."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session, select
from sqlalchemy import func, select as sa_select, cast as sa_cast, Integer
from typing import Optional

from app.db.database import get_session, engine
from app.db.models import (
    AppSetting, WantedGame, HuntStatus, HuntAttempt,
    Download, DownloadStatus, LibraryEntry, AppLog, InstalledExtension,
)
from app.db import repository
from app.services import sources as source_registry
from app.services.cover_sources import registry as cover_source_registry
from app.services.download_clients import registry as download_client_registry
from app.services.ra_client import SYSTEMS, RA_UNSUPPORTED_SYSTEMS
from app.services.title_utils import clean_title, canonical_system
from app.services import logger as applog

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


@router.get("/diag/rahasher")
async def diag_rahasher(system: str = "PlayStation", verify: bool = False,
                        session: Session = Depends(get_session)):
    """Validate a system's RA hashing: run RAHasher on one no_ra entry for
    `system` and compare to the stored hash. With verify=true, do a SINGLE RA
    lookup of the freshly-computed hash to confirm RA actually accepts it (one
    RA call — use to validate each system before mass re-hashing)."""
    import time
    from pathlib import Path
    from app.services.rahasher import compute_ra_hash, _rahasher_available, get_ra_system_id

    e = session.exec(
        select(LibraryEntry).where(
            LibraryEntry.system == system,
            LibraryEntry.ra_matched == False,        # noqa: E712
            LibraryEntry.file_hash != None,          # noqa: E711
        ).limit(1)
    ).first()
    if not e:
        return {"error": f"no unmatched hashed entry for system {system!r}"}

    p = Path(e.file_path)
    info = {
        "file": e.file_name, "system": e.system, "file_path": e.file_path,
        "exists": p.exists(),
        "size_mb": round(p.stat().st_size / 1e6, 1) if p.exists() else None,
        "stored_hash": e.file_hash,
        "rahasher_available": _rahasher_available(),
        "ra_system_id": get_ra_system_id(e.system),
    }
    if p.exists():
        t0 = time.time()
        ra_hash = await compute_ra_hash(p, e.system)
        info["rahasher_secs"] = round(time.time() - t0, 1)
        info["rahasher_hash"] = ra_hash
        info["stored_matches_rahasher"] = (ra_hash == e.file_hash) if ra_hash else None
        info["verdict"] = (
            "RAHasher failed → stored is a plain-MD5 fallback; re-hash needed" if ra_hash is None
            else "stored hash is stale/wrong; re-hash will fix" if ra_hash != e.file_hash
            else "RAHasher hash == stored but RA has no match (dump genuinely not in RA?)"
        )
        # Optional single RA lookup to PROVE the recomputed hash is RA-accepted.
        if verify and ra_hash:
            from app.services.ra_client import RAClient
            u = _get_setting(session, "ra_username")
            k = _get_setting(session, "ra_api_key")
            if u and k:
                try:
                    match = await RAClient(u, k).lookup_hash(ra_hash)
                    info["ra_lookup_matched"] = bool(match and match.get("ID"))
                    info["ra_matched_game_id"] = match.get("ID") if match else None
                    info["system_verdict"] = (
                        "HASHING CORRECT — RA accepts the recomputed hash"
                        if (match and match.get("ID")) else
                        "HASHING SUSPECT — recomputed hash still not in RA (dump variant, or this system needs a different input/algorithm)"
                    )
                except Exception as exc:
                    info["ra_lookup_error"] = str(exc)
            else:
                info["ra_lookup"] = "no RA credentials"
    return info


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
    existing = repository.wanted_by_ra_game_id(session, req.ra_game_id)
    if existing:
        return {"status": "exists", "id": existing.id, "game_title": existing.game_title}

    # Resolve a canonical system name server-side: the RA console id is
    # authoritative; otherwise normalize a possibly-doubled scraped string
    # ("WiiWii" -> "Wii"). This fixes the corruption at the source regardless of
    # what the Chrome extension posts.
    system = canonical_system(req.system, req.system_id)
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
        # Pass the game's title + canonical system (NOT the RA creds): _fetch_cover's
        # signature is (wanted_id, ra_game_id, game_title, system). Passing username
        # as game_title made title-based cover sources (SteamGridDB) fetch one constant
        # image for every extension-added game.
        background_tasks.add_task(_fetch_cover, game.id, req.ra_game_id, game.game_title, system)

    return {"status": "added", "id": game.id, "game_title": game.game_title}


@router.get("/game-status")
async def api_game_status(ra_game_id: int, session: Session = Depends(get_session)):
    """Pre-check for the browser extension: is this RA game already in the Wanted
    list and/or owned? Lets the panel reflect state on load instead of only after a
    POST. `owned` keys off a LibraryEntry matched to the id — RA files many ROMs
    under one game id and an unverified on-disk dump carries no ra_game_id, so this
    is the "verified copy on hand" signal, not raw file presence."""
    wanted = repository.wanted_by_ra_game_id(session, ra_game_id)
    owned = session.exec(
        select(LibraryEntry).where(LibraryEntry.ra_game_id == ra_game_id).limit(1)
    ).first()
    return {
        "ra_game_id": ra_game_id,
        "wanted": wanted is not None,
        "wanted_status": wanted.status.value if wanted else None,
        "owned": owned is not None,
    }


# ---------------------------------------------------------------------------
# Goals (browser extension: add a game- or achievement-level goal with a deadline)
# ---------------------------------------------------------------------------

class GoalAddRequest(BaseModel):
    ra_game_id: int
    game_title: str
    system: str = ""
    system_id: Optional[int] = None
    objective: str = "beaten"        # master | beaten | achievement
    achievement_id: Optional[int] = None
    achievement_title: str = ""
    event_name: str = ""
    deadline: str = ""               # YYYY-MM-DD; "" = no deadline


@router.post("/goal")
async def api_add_goal(
    req: GoalAddRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Create a goal from the browser extension. Used for both a game objective
    (master/beat from a /game/ page) and an achievement objective (from an
    /achievement/ page). achievement goals require achievement_id; they auto-complete
    on a HARDCORE unlock (services/goals.evaluate_goals)."""
    from app.db.models import Goal, GoalObjective, GoalStatus

    objective = req.objective if req.objective in (
        GoalObjective.master, GoalObjective.beaten, GoalObjective.achievement
    ) else GoalObjective.beaten
    if objective == GoalObjective.achievement and not req.achievement_id:
        return {"status": "error", "error": "achievement_id required for an achievement goal"}

    # De-dup: same game + objective (+ achievement) already tracked → report it.
    conds = [Goal.ra_game_id == req.ra_game_id, Goal.objective == objective]
    conds.append(Goal.achievement_id == req.achievement_id if req.achievement_id
                 else Goal.achievement_id == None)  # noqa: E711
    existing = session.exec(select(Goal).where(*conds)).first()
    if existing:
        return {"status": "exists", "id": existing.id, "objective": objective}

    system = canonical_system(req.system, req.system_id)
    goal = Goal(
        game_title=clean_title(req.game_title),
        system=system,
        ra_game_id=req.ra_game_id,
        achievement_id=req.achievement_id,
        objective=objective,
        custom_text=req.achievement_title.strip() if objective == GoalObjective.achievement else "",
        event_name=req.event_name.strip(),
        deadline=_parse_goal_deadline(req.deadline),
    )
    cover_file = Path(_get_setting(session, "covers_dir", "static/covers")) / f"{req.ra_game_id}.png"
    if cover_file.exists():
        goal.cover_path = f"covers/{req.ra_game_id}.png"
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("add_goal_extension", {
        "game": goal.game_title, "objective": objective,
        "achievement_id": req.achievement_id, "id": goal.id,
    })

    if not goal.cover_path and _get_setting(session, "ra_username") and _get_setting(session, "ra_api_key"):
        from app.routers.goals import _fetch_cover_goal
        background_tasks.add_task(_fetch_cover_goal, goal.id, req.ra_game_id, goal.game_title, system)

    return {"status": "added", "id": goal.id, "objective": objective}


@router.get("/goal-status")
async def api_goal_status(
    ra_game_id: int,
    achievement_id: Optional[int] = None,
    session: Session = Depends(get_session),
):
    """Pre-check for the extension: is this game/achievement already a goal? On an
    achievement page pass achievement_id; on a game page omit it (matches any
    game-level goal — master/beat — for the game)."""
    from app.db.models import Goal, GoalStatus

    q = select(Goal).where(Goal.ra_game_id == ra_game_id)
    if achievement_id is not None:
        q = q.where(Goal.achievement_id == achievement_id)
    else:
        q = q.where(Goal.achievement_id == None)  # noqa: E711
    goal = session.exec(q).first()
    return {
        "goal": goal is not None,
        "completed": bool(goal and goal.status == GoalStatus.completed),
        "objective": goal.objective if goal else None,
    }


def _parse_goal_deadline(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


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
):
    if not q:
        return []

    system = canonical_system(system, None)  # fix doubled scraped system ("WiiWii" -> "Wii")
    # Read enabled sources in a short session and release it BEFORE the slow
    # per-source searches — holding the connection across these awaits is what
    # starved the pool. The searches below need no DB connection.
    with Session(engine) as session:
        enabled_ids = _enabled_source_ids(session)
    results = []
    for src in source_registry.enabled_sources(enabled_ids):
        try:
            items = await src.search(q, system)
            for item in items:
                item["_source_name"] = src.name
            results.extend(items)
        except Exception as exc:
            # Don't abort the whole search for one bad source — but log it,
            # don't swallow silently (this masked real 403/429/network failures).
            applog.log_search(getattr(src, "name", "?"), q, system, 0, str(exc))
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
            "no_ra": _count(session, LibraryEntry, LibraryEntry.file_hash != None, LibraryEntry.ra_matched == False, LibraryEntry.system.not_in(RA_UNSUPPORTED_SYSTEMS)),  # noqa: E711,E712
            "unsupported": _count(session, LibraryEntry, LibraryEntry.system.in_(RA_UNSUPPORTED_SYSTEMS)),
            "duplicates": _count(session, LibraryEntry, LibraryEntry.duplicate_of != None),  # noqa: E711
            "mastered": _count(session, LibraryEntry, LibraryEntry.ra_award == "mastered"),
            "beaten": _count(session, LibraryEntry, LibraryEntry.ra_award.in_(["beaten", "beaten-softcore", "completed"])),
            "subset_compatible": _count(session, LibraryEntry, LibraryEntry.subset_info != ""),
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

    # RA dashboard mirror state (counts of the local mirror + last manual sync).
    try:
        from app.db.models import RAAchievement, RAGameProgress
        status["dashboard"] = {
            "achievements": _count(session, RAAchievement),
            "games": _count(session, RAGameProgress),
            "last_sync": _get_setting(session, "ra_dashboard_last_sync", ""),
        }
    except Exception as e:
        status["dashboard"] = {"error": str(e)}

    # Goals — event objectives + their deadline/completion state.
    try:
        from app.db.models import Goal, GoalStatus, GoalObjective
        now = datetime.utcnow()
        status["goals"] = {
            "total": _count(session, Goal),
            "active": _count(session, Goal, Goal.status == GoalStatus.active),
            "completed": _count(session, Goal, Goal.status == GoalStatus.completed),
            "overdue": _count(session, Goal, Goal.status == GoalStatus.active,
                              Goal.deadline != None, Goal.deadline < now),  # noqa: E711
            "custom": _count(session, Goal, Goal.objective == GoalObjective.custom),
            "achievement": _count(session, Goal, Goal.objective == GoalObjective.achievement),
        }
    except Exception as e:
        status["goals"] = {"error": str(e)}

    # Hash-aware subset cache state (RA-backed, cached locally).
    try:
        from app.db.models import RASubsetHash
        subset_avail = 0
        for e in session.exec(select(LibraryEntry).where(LibraryEntry.subset_info != "")).all():
            try:
                info = json.loads(e.subset_info)
            except (ValueError, TypeError):
                continue
            if any(not s.get("mastered") for s in info):
                subset_avail += 1
        status["subsets"] = {
            "cached_hashes": _count(session, RASubsetHash),
            "subset_available": subset_avail,
            "last_sync": _get_setting(session, "subset_cache_last_sync", ""),
        }
    except Exception as e:
        status["subsets"] = {"error": str(e)}

    # External torrent/usenet jobs (qBittorrent/SABnzbd via Prowlarr download client).
    try:
        from app.db.models import ExternalDownload
        rows = session.exec(sa_select(ExternalDownload.status)).all()
        active = sum(1 for s in rows if s in ("submitted", "metadata", "downloading", "verifying"))
        status["external"] = {
            "active": active,
            "verified": sum(1 for s in rows if s == "verified"),
            "failed": sum(1 for s in rows if s == "failed"),
            "clients": [c.client_id for c in download_client_registry.all_clients()],
        }
    except Exception as e:
        status["external"] = {"error": str(e)}

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


# ---------------------------------------------------------------------------
# Live change-signal (front-end auto-update backbone)
#
# /api/changes returns a cheap per-scope fingerprint string. The base.html
# poller compares the scopes a page cares about against the previous poll; when
# one changes it morphs the page in place (idiomorph), so background work shows
# up without a manual refresh and without losing the user's scroll/selection.
# Tokens only need to CHANGE when something user-visible changes — collisions
# just defer the update to the next real change; spurious changes cause a
# harmless no-op morph. Each scope is independently guarded so one failure can't
# blank the whole feed. NO network I/O here — fast DB reads only, so holding the
# pooled connection for the request is fine.
# ---------------------------------------------------------------------------

@router.get("/changes")
async def api_changes(session: Session = Depends(get_session)):
    changes: dict = {}

    # library — LibraryEntry has no single updated_at (cover/award/subset/dup
    # writes don't bump one), so fingerprint the visible aggregate in one query.
    try:
        row = session.execute(
            sa_select(
                func.count(),
                func.count(LibraryEntry.file_hash),
                # cast bool→int so the SUM stays a true count (SQLAlchemy otherwise
                # applies Boolean result-processing and collapses it to True/False).
                func.coalesce(func.sum(sa_cast(LibraryEntry.ra_matched, Integer)), 0),
                func.coalesce(func.sum(func.length(LibraryEntry.cover_path)), 0),
                func.count(LibraryEntry.duplicate_of),
                func.coalesce(func.sum(sa_cast(LibraryEntry.missing, Integer)), 0),
                func.coalesce(func.sum(LibraryEntry.save_count), 0),
                func.coalesce(func.sum(func.length(LibraryEntry.ra_award)), 0),
                func.coalesce(func.sum(func.length(LibraryEntry.subset_info)), 0),
                func.max(LibraryEntry.added_at),
                func.max(LibraryEntry.hashed_at),
                func.max(LibraryEntry.ra_checked_at),
                func.max(LibraryEntry.missing_at),
            ).select_from(LibraryEntry)
        ).one()
        changes["library"] = ":".join(str(v) for v in row)
    except Exception as e:
        changes["library"] = f"err:{e}"

    # goals — count + completed count + cover state + last touch (add/complete/reopen/
    # edit/auto-complete all bump updated_at; a background cover write only sets
    # cover_path, hence the length sum, mirroring the wanted scope).
    try:
        from app.db.models import Goal, GoalStatus
        total = _count(session, Goal)
        completed = _count(session, Goal, Goal.status == GoalStatus.completed)
        cover_len = session.scalar(sa_select(func.coalesce(func.sum(func.length(Goal.cover_path)), 0)))
        last_touch = session.scalar(sa_select(func.max(Goal.updated_at)))
        changes["goals"] = f"{total}:{completed}:{cover_len}:{last_touch}"
    except Exception as e:
        changes["goals"] = f"err:{e}"

    # wanted — count + last touch + cover state (status→verified bumps updated_at;
    # cover fetch only writes cover_path, hence the length sum).
    try:
        row = session.execute(
            sa_select(
                func.count(),
                func.max(WantedGame.updated_at),
                func.coalesce(func.sum(func.length(WantedGame.cover_path)), 0),
            ).select_from(WantedGame)
        ).one()
        changes["wanted"] = ":".join(str(v) for v in row)
    except Exception as e:
        changes["wanted"] = f"err:{e}"

    # downloads — structural only (new item, reached pending_approval, failed,
    # hash computed). Progress is DELIBERATELY excluded so this token doesn't tick
    # every 2s during a download; the per-item /downloads/{id}/status poll drives
    # the live progress bars.
    try:
        total = _count(session, Download)
        pending_approval = _count(session, Download, Download.status == DownloadStatus.pending_approval)
        failed = _count(session, Download, Download.status == DownloadStatus.failed)
        hashed = _count(session, Download, Download.file_hash != None)  # noqa: E711
        last_created = session.scalar(sa_select(func.max(Download.created_at)))
        changes["downloads"] = f"{total}:{pending_approval}:{failed}:{hashed}:{last_created}"
    except Exception as e:
        changes["downloads"] = f"err:{e}"

    # hunts / logs — append-only tables: count + max(id) is enough.
    try:
        total = _count(session, HuntAttempt)
        last_id = session.scalar(sa_select(func.max(HuntAttempt.id)))
        changes["hunts"] = f"{total}:{last_id}"
    except Exception as e:
        changes["hunts"] = f"err:{e}"

    # external — torrent/usenet jobs change state on the scheduler poll, so the
    # downloads page morphs when their status/count moves (progress excluded, like
    # downloads — the per-item poll drives the bar).
    try:
        from app.db.models import ExternalDownload
        total = _count(session, ExternalDownload)
        active = _count(session, ExternalDownload,
                        ExternalDownload.status.in_(("submitted", "metadata", "downloading", "verifying")))
        last_touch = session.scalar(sa_select(func.max(ExternalDownload.updated_at)))
        changes["external"] = f"{total}:{active}:{last_touch}"
    except Exception as e:
        changes["external"] = f"err:{e}"

    try:
        total = _count(session, AppLog)
        last_id = session.scalar(sa_select(func.max(AppLog.id)))
        changes["logs"] = f"{total}:{last_id}"
    except Exception as e:
        changes["logs"] = f"err:{e}"

    # scheduler — the three last-run stamps; change when a scheduled task fires.
    try:
        changes["scheduler"] = ":".join(
            _get_setting(session, f"sched_{t}_last_run", "") for t in ("scan", "hash", "autodiscover")
        )
    except Exception as e:
        changes["scheduler"] = f"err:{e}"

    # dashboard — the local RA mirror; only changes after a manual Refresh completes.
    try:
        from app.db.models import RAAchievement, RAGameProgress
        ach = _count(session, RAAchievement)
        games = _count(session, RAGameProgress)
        last_sync = _get_setting(session, "ra_dashboard_last_sync", "")
        changes["dashboard"] = f"{ach}:{games}:{last_sync}"
    except Exception as e:
        changes["dashboard"] = f"err:{e}"

    return changes
