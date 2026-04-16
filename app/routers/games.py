from fastapi import APIRouter, Request, Query, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlmodel import Session

from app.db.database import get_session
from app.db.models import AppSetting
from app.services import sources as source_registry
from app.services.ra_client import SYSTEMS, RAClient

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _enabled_source_ids(session: Session) -> set[str]:
    """Return the set of source IDs the user has enabled."""
    enabled = set()
    for src in source_registry.all_sources():
        key = f"source_{src.source_id}_enabled"
        if _get_setting(session, key, "false") == "true":
            enabled.add(src.source_id)
    return enabled


def _get_ra_client(session: Session) -> RAClient | None:
    if _get_setting(session, "ra_enabled") != "true":
        return None
    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if not username or not api_key:
        return None
    return RAClient(username, api_key)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: Session = Depends(get_session)):
    enabled_ids = _enabled_source_ids(session)
    all_srcs = source_registry.all_sources()
    return templates.TemplateResponse(
        request, "index.html",
        {"systems": SYSTEMS, "sources": all_srcs, "enabled_source_ids": enabled_ids},
    )


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = Query(default=""),
    system: str = Query(default=""),
    source_id: str = Query(default=""),
    session: Session = Depends(get_session),
):
    """HTMX: search enabled sources and return a results partial."""
    results = []
    error = None

    if q:
        # Determine which sources to query
        enabled_ids = _enabled_source_ids(session)
        if source_id and source_id in enabled_ids:
            srcs = [source_registry.get(source_id)]
        else:
            srcs = source_registry.enabled_sources(enabled_ids)

        try:
            for src in srcs:
                if src is None:
                    continue
                src_results = await src.search(q, system)
                results.extend(src_results)
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
    source_id: str = Query(default="archive_org"),
):
    """HTMX: list ROM files inside a source result."""
    files = []
    error = None

    src = source_registry.get(source_id)
    if src is None:
        error = f"Unknown source: {source_id}"
    else:
        try:
            files = await src.get_files(identifier, name_filter=q)
        except Exception as exc:
            error = str(exc)

    return templates.TemplateResponse(
        request, "partials/file_list.html",
        {
            "files": files,
            "identifier": identifier,
            "query": q,
            "system": system,
            "game_title": game_title,
            "source_id": source_id,
            "error": error,
        },
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
    """HTMX: fetch RA hashes, then search enabled sources for matching collections."""
    ra = _get_ra_client(session)
    if not ra:
        return HTMLResponse('<p class="text-red-400 text-sm">RA not configured.</p>')

    error = None
    hashes = []
    collections = []
    rom_names = []

    try:
        hashes = await ra.get_game_hashes_full(game_id)
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

        if rom_names:
            first_stem = Path(rom_names[0]["name"]).stem
            enabled_ids = _enabled_source_ids(session)
            for src in source_registry.enabled_sources(enabled_ids):
                try:
                    results = await src.search(first_stem, system_name)
                    for r in results:
                        r["_source_name"] = src.name
                    collections.extend(results)
                except Exception:
                    pass
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
