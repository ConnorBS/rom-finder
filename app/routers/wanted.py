from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks
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

router = APIRouter(prefix="/wanted")
templates = Jinja2Templates(directory="app/templates")

RA_MEDIA_BASE = "https://media.retroachievements.org"


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _get_ra_client(session: Session) -> RAClient | None:
    if _get_setting(session, "ra_enabled") != "true":
        return None
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
        return HTMLResponse(
            f'<span class="text-gray-500 text-xs">Already in Wanted</span>'
            f'<a href="/wanted" class="text-blue-400 text-xs hover:underline ml-2">View ↗</a>'
        )

    game = WantedGame(game_title=game_title, system=system, ra_game_id=ra_game_id)
    session.add(game)
    session.commit()
    session.refresh(game)

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
    wanted = session.get(WantedGame, game_id)
    if not wanted:
        return HTMLResponse('<p class="text-red-400 text-xs">Not found.</p>')

    ra = _get_ra_client(session)
    rom_names: list[dict] = []
    collections: list[dict] = []
    error: str | None = None

    try:
        # Get canonical ROM file names from RA if available
        if ra:
            hashes = await ra.get_game_hashes_full(wanted.ra_game_id)
            seen: set[str] = set()
            for h in hashes:
                name = h.get("Name", "")
                if name and name not in seen:
                    rom_names.append({
                        "name": name,
                        "md5": h.get("MD5", ""),
                        "labels": h.get("Labels", []),
                    })
                    seen.add(name)

        # Use the game title as the search query — RA ROM filenames use
        # No-Intro naming (e.g. "Game (Europe) (En,Fr,De)") which doesn't
        # match how sources like VIMM title their entries.
        search_query = wanted.game_title
        enabled_ids = _enabled_source_ids(session)
        for src in source_registry.enabled_sources(enabled_ids):
            try:
                results = await src.search(search_query, wanted.system)
                for r in results:
                    r["_source_name"] = src.name
                collections.extend(results)
            except Exception:
                pass
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request, "partials/wanted_sources.html",
        {
            "wanted": wanted,
            "rom_names": rom_names,
            "collections": collections,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _fetch_cover(wanted_id: int, ra_game_id: int, username: str, api_key: str) -> None:
    """Download cover art from RA media CDN and cache it locally."""
    from app.db.database import engine
    from sqlmodel import Session as SyncSession

    try:
        ra = RAClient(username, api_key)
        info = await ra.get_game_info(ra_game_id)
        icon = info.get("ImageIcon", "")
        if not icon:
            return

        cover_url = f"{RA_MEDIA_BASE}{icon}"
        cover_dir = Path("static/covers")
        cover_dir.mkdir(parents=True, exist_ok=True)
        cover_path = cover_dir / f"{ra_game_id}.png"

        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(cover_url)
            if resp.status_code == 200:
                cover_path.write_bytes(resp.content)
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
