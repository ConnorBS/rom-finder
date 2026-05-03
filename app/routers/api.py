"""JSON API for the Chrome extension (and any other external clients)."""

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlmodel import Session, select
from typing import Optional

from app.db.database import get_session
from app.db.models import AppSetting, WantedGame
from app.services import sources as source_registry
from app.services.ra_client import SYSTEMS
from app.services.title_utils import clean_title

router = APIRouter(prefix="/api")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


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
