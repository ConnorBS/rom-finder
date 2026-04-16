from fastapi import APIRouter, Request, Query, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlmodel import Session

from app.db.database import get_session
from app.db.models import AppSetting
from app.services.archive_client import ArchiveClient
from app.services.ra_client import SYSTEMS, RAClient

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _get_ra_client(session: Session) -> RAClient | None:
    """Return an RAClient if RA is enabled and configured, else None."""
    if _get_setting(session, "ra_enabled") != "true":
        return None
    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if not username or not api_key:
        return None
    return RAClient(username, api_key)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"systems": SYSTEMS})


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = Query(default=""),
    system: str = Query(default=""),
):
    """HTMX endpoint: search archive.org and return a results partial."""
    results = []
    error = None

    if q:
        try:
            client = ArchiveClient()
            results = await client.search_collections(q, system)
        except Exception as exc:
            error = str(exc)

    return templates.TemplateResponse(
        request, "partials/search_results.html",
        {"results": results, "query": q, "error": error},
    )


@router.get("/games/{identifier}/files", response_class=HTMLResponse)
async def browse_files(
    request: Request,
    identifier: str,
    q: str = Query(default=""),
    system: str = Query(default=""),
    game_title: str = Query(default=""),
):
    """HTMX endpoint: list ROM files inside an archive.org item."""
    files = []
    error = None

    try:
        client = ArchiveClient()
        files = await client.get_files(identifier, name_filter=q)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request, "partials/file_list.html",
        {"files": files, "identifier": identifier, "query": q,
         "system": system, "game_title": game_title, "error": error},
    )


@router.get("/ra/search", response_class=HTMLResponse)
async def ra_search(
    request: Request,
    system_id: int = Query(...),
    q: str = Query(default=""),
    session: Session = Depends(get_session),
):
    """HTMX: search RA game list for a system by title."""
    ra = _get_ra_client(session)
    if not ra:
        return HTMLResponse(
            '<p class="text-yellow-500 text-sm">RetroAchievements is not configured. '
            'Enable it in <a href="/settings" class="underline">Settings</a>.</p>'
        )

    games = []
    error = None
    system_name = SYSTEMS.get(system_id, "")

    if q:
        try:
            games = await ra.search_games(system_id, q)
        except Exception as exc:
            error = str(exc)

    return templates.TemplateResponse(
        request, "partials/ra_game_results.html",
        {"games": games, "query": q, "system_name": system_name, "error": error},
    )


@router.get("/ra/games/{game_id}/sources", response_class=HTMLResponse)
async def ra_game_sources(
    request: Request,
    game_id: int,
    game_title: str = Query(default=""),
    system_name: str = Query(default=""),
    session: Session = Depends(get_session),
):
    """HTMX: fetch RA hashes for a game, then search archive.org for matching collections."""
    ra = _get_ra_client(session)
    if not ra:
        return HTMLResponse('<p class="text-red-400 text-sm">RA not configured.</p>')

    error = None
    hashes = []
    collections = []
    rom_names = []

    try:
        hashes = await ra.get_game_hashes_full(game_id)
        # Collect unique ROM file names from RA hash entries
        seen = set()
        for h in hashes:
            name = h.get("Name", "")
            if name and name not in seen:
                rom_names.append({"name": name, "md5": h.get("MD5", ""), "labels": h.get("Labels", [])})
                seen.add(name)

        if rom_names:
            # Use the first ROM name (strip extension) as the archive.org search query
            first_name = Path(rom_names[0]["name"]).stem
            archive = ArchiveClient()
            collections = await archive.search_collections(first_name, system_name)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request, "partials/ra_sources.html",
        {
            "game_id": game_id,
            "game_title": game_title,
            "system_name": system_name,
            "rom_names": rom_names,
            "collections": collections,
            "error": error,
        },
    )
