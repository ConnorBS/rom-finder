from fastapi import APIRouter, Request, Query, Depends, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlmodel import Session

from app.db.database import get_session
from app.db.models import AppSetting
from app.services import sources as source_registry
from app.services.ra_client import SYSTEMS, RAClient
from app.services.title_utils import search_variations, stem_from_rom_name
from app.services import logger as applog

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
    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if not username or not api_key:
        return None
    return RAClient(username, api_key)


@router.get("/covers/{filename}")
async def serve_cover(filename: str, session: Session = Depends(get_session)):
    """Serve a cover image from the configured covers_dir."""
    covers_dir = _get_setting(session, "covers_dir", "static/covers")
    file_path = Path(covers_dir) / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(file_path)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: Session = Depends(get_session)):
    enabled_ids = _enabled_source_ids(session)
    all_srcs = source_registry.all_sources()
    applog.log_navigation("home", {"enabled_sources": list(enabled_ids)})
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

    if q:
        applog.log_search(
            source_id or "all",
            q,
            system,
            len(results),
            error or "",
        )

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
    ra_game_id: int = Query(default=0),
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

    applog.verbose("navigation", f"Browse files: {identifier}", {
        "identifier": identifier, "source": source_id, "game": game_title,
        "system": system, "file_count": len(files), "error": error,
    })

    return templates.TemplateResponse(
        request, "partials/file_list.html",
        {
            "files": files,
            "identifier": identifier,
            "query": q,
            "system": system,
            "game_title": game_title,
            "source_id": source_id,
            "ra_game_id": ra_game_id,
            "error": error,
        },
    )


@router.get("/ra/search", response_class=HTMLResponse)
async def ra_search(
    request: Request,
    system_id: str = Query(default=""),
    q: str = Query(default=""),
    mode: str = Query(default="lookup"),
    session: Session = Depends(get_session),
):
    """HTMX: search RA game list for a system by title.
    mode='add'    → 'Add to Wanted' button (Wanted page)
    mode='lookup' → 'Find Sources' button (default)
    """
    ra = _get_ra_client(session)
    if not ra:
        return HTMLResponse(
            '<p class="text-yellow-500 text-sm">Add your RetroAchievements credentials in '
            '<a href="/settings" class="underline">Settings</a> to search games.</p>'
        )

    # system_id arrives as an empty string when nothing is selected
    sid: int | None = int(system_id) if system_id.strip() else None

    games = []
    error = None
    system_name = SYSTEMS.get(sid, "") if sid else ""

    if q and not sid:
        return HTMLResponse(
            '<p class="text-gray-500 text-sm">Select a console above to search.</p>'
        )

    if q and sid:
        try:
            games = await ra.search_games(sid, q)
        except Exception as exc:
            error = str(exc)

    if q:
        applog.verbose("search", f"RA game search: \"{q}\" [{system_name}] → {len(games)} result(s)", {
            "query": q, "system_id": sid, "system": system_name,
            "results": len(games), "mode": mode, "error": error,
        })

    return templates.TemplateResponse(
        request, "partials/ra_game_results.html",
        {"games": games, "query": q, "system_name": system_name, "mode": mode, "error": error},
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

        # Build search query candidates: ROM filename stems first, then title variants
        queries: list[str] = []
        seen_queries: set[str] = set()
        for rom in rom_names[:3]:  # use up to 3 ROM names
            stem = stem_from_rom_name(rom["name"])
            if stem and stem not in seen_queries:
                queries.append(stem)
                seen_queries.add(stem)
        for variant in search_variations(game_title):
            if variant not in seen_queries:
                queries.append(variant)
                seen_queries.add(variant)

        seen_ids: set[str] = set()
        enabled_ids = _enabled_source_ids(session)
        for query in queries:
            for src in source_registry.enabled_sources(enabled_ids):
                try:
                    results = await src.search(query, system_name)
                    for r in results:
                        uid = r.get("identifier", r.get("title", ""))
                        if uid not in seen_ids:
                            seen_ids.add(uid)
                            r["_source_name"] = src.name
                            collections.append(r)
                except Exception:
                    pass
            if collections:
                break  # stop trying more query variants once we have results
    except Exception as exc:
        error = str(exc)

    applog.verbose("source", f"RA sources lookup: {game_title} → {len(collections)} collection(s)", {
        "game_id": game_id, "game_title": game_title, "system": system_name,
        "rom_names": [r["name"] for r in rom_names], "collections": len(collections), "error": error,
    })

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
