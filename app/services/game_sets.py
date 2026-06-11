"""RA V2 multiset (achievement-set) awareness — runs ALONGSIDE the V1 subset cache
(services/subsets.py), it does not replace it.

For each owned game, one V2 `/games/{id}?include=achievementSets,hashes` call enumerates
its achievement SETS within the single game id (selected by `?set=`), classifying each
as base-`compatible` (bonus/challenge — the base ROM plays it) or `patch-required`
(specialty/exclusive — needs RA's compatibility patch, with the `patch_url`). Cached in
`ra_game_set` and shown read-only in the collection detail panel. Best-effort: a game whose
V2 fetch fails is skipped; V2 being down never breaks the V1 subset flow.
"""
from datetime import datetime

from sqlmodel import Session, select, text

from app.db.database import engine
from app.db.models import LibraryEntry, RAGameSet
from app.services import settings as app_settings
from app.services import logger as applog
from app.services.ra_client_v2 import RAClientV2


async def refresh_game_sets(game_ids: list[int] | None = None) -> dict:
    """Refresh the V2 achievement-set cache. `game_ids=None` → every owned game (one
    V2 call each, globally rate-limited to 2 req/s); a list → only those. Replaces rows
    per game_id. Opens its own sessions; never holds one across the RA await."""
    from app.services import activity as activity_store

    with Session(engine) as s:
        api_key = app_settings.get(s, "ra_api_key")
        if not api_key:
            return {"status": "no_credentials", "games": 0, "sets": 0}
        owned = sorted({e.ra_game_id for e in s.exec(
            select(LibraryEntry).where(LibraryEntry.ra_game_id != None)  # noqa: E711
        ).all() if e.ra_game_id})

    if game_ids is not None:
        gid_set = set(game_ids)
        owned = [g for g in owned if g in gid_set]
    if not owned:
        return {"status": "ok", "games": 0, "sets": 0}

    ra = RAClientV2(api_key)
    activity_store.start_batch("game-sets-sync", "Achievement sets (V2)", len(owned), task_type="task")
    total_sets = 0
    try:
        for gid in owned:
            try:
                data = await ra.get_game(gid, include="achievementSets,hashes")
            except Exception as exc:
                applog.verbose("system", f"V2 game-sets fetch skipped (game {gid}): {exc}", {})
                activity_store.increment("game-sets-sync")
                continue
            sets = RAClientV2.sets_from_game(data)
            with Session(engine) as s:
                s.exec(text(f"DELETE FROM ra_game_set WHERE game_id = {int(gid)}"))
                for st in sets:
                    s.add(RAGameSet(
                        game_id=gid, set_id=st["set_id"], title=st["title"],
                        set_type=st["set_type"], compatibility=st["compatibility"],
                        patch_url=st["patch_url"], points_total=st["points_total"],
                        updated_at=datetime.utcnow(),
                    ))
                s.commit()
            total_sets += len(sets)
            activity_store.increment("game-sets-sync")
    finally:
        activity_store.finish("game-sets-sync")

    applog.info("system", "V2 achievement sets refreshed", {"games": len(owned), "sets": total_sets})
    return {"status": "ok", "games": len(owned), "sets": total_sets}


def game_sets_for(session: Session, game_id: int) -> list[dict]:
    """Cached V2 sets for one owned game (collection detail panel)."""
    rows = session.exec(
        select(RAGameSet).where(RAGameSet.game_id == game_id).order_by(RAGameSet.title)
    ).all()
    return [{
        "set_id": r.set_id, "title": r.title, "set_type": r.set_type,
        "compatibility": r.compatibility, "patch_url": r.patch_url, "points_total": r.points_total,
    } for r in rows]
