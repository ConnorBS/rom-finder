from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from pathlib import Path
from datetime import datetime

import httpx

from app.db.database import get_session
from app.db.models import AppSetting, WantedGame, HuntStatus
from app.services import sources as source_registry
from app.services.ra_client import SYSTEMS, RAClient
from app.services.title_utils import search_variations, stem_from_rom_name
from app.services import logger as applog

router = APIRouter(prefix="/wanted")
templates = Jinja2Templates(directory="app/templates")

RA_MEDIA_BASE = "https://media.retroachievements.org"


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _get_ra_client(session: Session) -> RAClient | None:
    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if not username or not api_key:
        return None
    return RAClient(username, api_key)


def _enabled_source_ids(session: Session) -> set[str]:
    enabled = set()
    for src in source_registry.all_sources():
        key = f"source_{src.source_id}_enabled"
        if _get_setting(session, key, "false") == "true":
            enabled.add(src.source_id)
    return enabled


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def wanted_page(request: Request, session: Session = Depends(get_session)):
    games = session.exec(
        select(WantedGame).order_by(WantedGame.added_at.desc())
    ).all()
    ra_configured = bool(_get_ra_client(session))
    applog.log_navigation("wanted", {"game_count": len(games), "ra_configured": ra_configured})
    return templates.TemplateResponse(
        request, "wanted.html",
        {"games": games, "systems": SYSTEMS, "ra_configured": ra_configured},
    )


# ---------------------------------------------------------------------------
# HTMX — add / remove
# ---------------------------------------------------------------------------

@router.post("/add", response_class=HTMLResponse)
async def add_wanted(
    request: Request,
    background_tasks: BackgroundTasks,
    ra_game_id: int = Form(...),
    game_title: str = Form(...),
    system: str = Form(...),
    session: Session = Depends(get_session),
):
    # Deduplicate
    existing = session.exec(
        select(WantedGame).where(WantedGame.ra_game_id == ra_game_id)
    ).first()
    if existing:
        applog.log_action_verbose("add_wanted_duplicate", {
            "game": game_title, "system": system, "ra_game_id": ra_game_id,
        })
        return HTMLResponse(
            f'<span class="text-gray-500 text-xs">Already in Wanted</span>'
            f'<a href="/wanted" class="text-blue-400 text-xs hover:underline ml-2">View ↗</a>'
        )

    game = WantedGame(game_title=game_title, system=system, ra_game_id=ra_game_id)
    session.add(game)
    session.commit()
    session.refresh(game)
    applog.log_action("add_wanted", {
        "game": game_title, "system": system, "ra_game_id": ra_game_id, "id": game.id,
    })

    # Grab RA credentials for the background cover task
    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if username and api_key:
        background_tasks.add_task(_fetch_cover, game.id, ra_game_id, username, api_key)

    return templates.TemplateResponse(
        request, "partials/wanted_added.html",
        {"game": game},
    )


@router.delete("/{game_id}", response_class=HTMLResponse)
async def remove_wanted(game_id: int, session: Session = Depends(get_session)):
    game = session.get(WantedGame, game_id)
    if game:
        applog.log_action("remove_wanted", {
            "id": game_id, "game": game.game_title, "system": game.system,
        })
        session.delete(game)
        session.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# HTMX — search sources for a specific wanted game
# ---------------------------------------------------------------------------

@router.get("/{game_id}/sources", response_class=HTMLResponse)
async def wanted_sources(
    request: Request,
    game_id: int,
    session: Session = Depends(get_session),
):
    """Return the source-search panel. Fetches RA hashes once, then renders one
    auto-loading section per enabled source so results trickle in in parallel."""
    wanted = session.get(WantedGame, game_id)
    if not wanted:
        return HTMLResponse('<p class="text-red-400 text-xs">Not found.</p>')

    ra = _get_ra_client(session)
    rom_names: list[dict] = []
    error: str | None = None

    try:
        if ra:
            hashes = await ra.get_game_hashes_full(wanted.ra_game_id)
            seen_names: set[str] = set()
            for h in hashes:
                name = h.get("Name", "")
                if name and name not in seen_names:
                    rom_names.append({
                        "name": name,
                        "md5": h.get("MD5", ""),
                        "labels": h.get("Labels", []),
                    })
                    seen_names.add(name)
    except Exception as exc:
        error = str(exc)

    # Build query list once here and pass to each per-source section as a
    # pipe-delimited URL param so each source doesn't re-fetch RA hashes.
    queries: list[str] = []
    seen_q: set[str] = set()
    for rom in rom_names[:3]:
        stem = stem_from_rom_name(rom["name"])
        if stem and stem not in seen_q:
            queries.append(stem)
            seen_q.add(stem)
    for variant in search_variations(wanted.game_title):
        if variant not in seen_q:
            queries.append(variant)
            seen_q.add(variant)

    enabled_ids = _enabled_source_ids(session)
    enabled_srcs = source_registry.enabled_sources(enabled_ids)
    queries_param = "|".join(queries)

    applog.info("navigation", f"Source search opened: {wanted.game_title}", {
        "game_id": game_id, "system": wanted.system,
        "queries": queries, "sources": [s.source_id for s in enabled_srcs],
    })

    return templates.TemplateResponse(
        request, "partials/wanted_sources.html",
        {
            "wanted": wanted,
            "rom_names": rom_names,
            "sources": enabled_srcs,
            "queries_param": queries_param,
            "error": error,
        },
    )


@router.get("/{game_id}/sources/{source_id}", response_class=HTMLResponse)
async def wanted_source_results(
    request: Request,
    game_id: int,
    source_id: str,
    queries: str = Query(default=""),
    system: str = Query(default=""),
    session: Session = Depends(get_session),
):
    """HTMX: search a single source for a wanted game. Fires in parallel for
    each source section via hx-trigger='load'."""
    wanted = session.get(WantedGame, game_id)
    src = source_registry.get(source_id)
    results: list[dict] = []
    error: str | None = None

    if src is None:
        error = f"Unknown source: {source_id}"
    else:
        query_list = [q for q in queries.split("|") if q]
        seen_ids: set[str] = set()

        for query in query_list:
            try:
                src_results = await src.search(query, system)
                for r in src_results:
                    uid = r.get("identifier", r.get("title", ""))
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        results.append(r)
            except Exception as exc:
                error = str(exc)
                break
            if results:
                break  # stop on first query that yields results

    applog.log_search(src.name if src else source_id, queries.split("|")[0] if queries else "", system, len(results), error or "")

    return templates.TemplateResponse(
        request, "partials/wanted_source_section.html",
        {
            "source": src,
            "source_id": source_id,
            "results": results,
            "error": error,
            "wanted": wanted,
            "rom_names": [],
        },
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _fetch_cover(wanted_id: int, ra_game_id: int, username: str, api_key: str) -> None:
    """Download cover art from RA media CDN and save to the configured covers_dir."""
    from app.db.database import engine
    from sqlmodel import Session as SyncSession
    from app.db.models import AppSetting

    try:
        ra = RAClient(username, api_key)
        info = await ra.get_game_info(ra_game_id)
        icon = info.get("ImageIcon", "")
        if not icon:
            return

        cover_url = f"{RA_MEDIA_BASE}{icon}"

        with SyncSession(engine) as s:
            setting = s.get(AppSetting, "covers_dir")
            covers_dir = Path(setting.value if setting else "static/covers")

        covers_dir.mkdir(parents=True, exist_ok=True)
        cover_file = covers_dir / f"{ra_game_id}.png"

        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(cover_url)
            if resp.status_code == 200:
                cover_file.write_bytes(resp.content)
            else:
                return
    except Exception:
        return

    with SyncSession(engine) as session:
        game = session.get(WantedGame, wanted_id)
        if game:
            game.cover_path = f"covers/{ra_game_id}.png"
            game.updated_at = datetime.utcnow()
            session.add(game)
            session.commit()
