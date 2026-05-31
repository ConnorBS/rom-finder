"""Hash-aware RetroAchievements subset detection.

Two layers:

1. `refresh_subset_cache()` — RA-backed (the only part that calls RA). For each owned
   game it enumerates that game's subsets from the per-console game list (the same
   `get_game_list` autodiscover already uses) and pulls each subset's accepted hash
   list, caching them in `ra_subset_hash`. Re-enumerating from the live list each run
   means subsets RA has *added* since last run are picked up automatically. Replaced
   wholesale on a full sweep, or per-parent when scoped to specific game ids.

2. `recompute_subset_flags()` — LOCAL (zero RA calls). Joins each owned ROM's hash
   against the cached subset hashes and writes `is_subset_rom` + `subset_info` (the
   compatible subsets, each flagged mastered/not from the dashboard mirror). This is
   what the collection reads, so the page itself makes no RA calls.

A ROM is matched to a subset purely by `md5 == file_hash` — a hash may be valid for
several subsets, and a subset needing a specific patched ROM the user doesn't own
simply never matches.
"""
import json
from datetime import datetime

from sqlmodel import Session, select, text

from app.db.database import engine
from app.db.models import LibraryEntry, RAGameProgress, RASubsetHash
from app.services import settings as app_settings
from app.services import logger as applog
from app.services.ra_client import RAClient, SYSTEMS
from app.services.duplicates import _is_subset, _SUBSET_RE
from app.services.mastery import base_title


def _is_subset_title(title: str) -> bool:
    return bool(_SUBSET_RE.search(title or ""))


async def refresh_subset_cache(game_ids: list[int] | None = None) -> dict:
    """Refresh the cached subset→hash map from RA. `game_ids=None` → all owned games
    (full sweep, wholesale replace); a list → only those games' subsets (scoped).
    Opens its own sessions; never holds one across an RA await. Ends by recomputing
    the local per-ROM flags."""
    from app.services import activity as activity_store

    with Session(engine) as s:
        username = app_settings.get(s, "ra_username")
        api_key = app_settings.get(s, "ra_api_key")
        if not (username and api_key):
            return {"status": "no_credentials", "subsets": 0, "hashes": 0}
        owned = s.exec(select(LibraryEntry).where(LibraryEntry.ra_game_id != None)).all()  # noqa: E711
        owned = [(e.ra_game_id, e.system) for e in owned]

    if game_ids is not None:
        gid_set = set(game_ids)
        owned = [(gid, sysname) for gid, sysname in owned if gid in gid_set]
    if not owned:
        return {"status": "ok", "parents": 0, "subsets": 0, "hashes": 0}

    name_to_id = {v: k for k, v in SYSTEMS.items()}
    consoles: dict[int, set[int]] = {}
    for gid, sysname in owned:
        cid = name_to_id.get(sysname)
        if cid:
            consoles.setdefault(cid, set()).add(gid)
    if not consoles:
        return {"status": "ok", "parents": 0, "subsets": 0, "hashes": 0}

    ra = RAClient(username, api_key)
    activity_store.start("subset-sync", "Subset cache", task_type="task")

    # parent_game_id -> {"console_id": int, "subsets": {subset_id: title}}
    fetch: dict[int, dict] = {}
    rows: list[tuple] = []   # (parent_game_id, subset_game_id, subset_title, console_id, md5)
    try:
        for cid, owned_ids in consoles.items():
            try:
                games = await ra.get_game_list(cid)
            except Exception as exc:
                applog.warning("system", f"Subset cache: game list failed for console {cid}: {exc}")
                continue
            title_by_id: dict[int, str] = {}
            groups: dict[str, dict] = {}     # base_title -> {"base_id": int|None, "subsets": {id:title}}
            for g in games:
                gid = g.get("ID")
                title = g.get("Title", "") or ""
                if not gid:
                    continue
                title_by_id[gid] = title
                grp = groups.setdefault(base_title(title), {"base_id": None, "subsets": {}})
                if _is_subset_title(title):
                    grp["subsets"][gid] = title
                else:
                    grp["base_id"] = gid
            for owned_id in owned_ids:
                title = title_by_id.get(owned_id)
                if not title:
                    continue
                grp = groups.get(base_title(title))
                if not grp or not grp["subsets"]:
                    continue
                parent = grp["base_id"] or owned_id
                ent = fetch.setdefault(parent, {"console_id": cid, "subsets": {}})
                ent["subsets"].update(grp["subsets"])

        hash_cache: dict[int, list[str]] = {}
        for parent, ent in fetch.items():
            cid = ent["console_id"]
            for sid, stitle in ent["subsets"].items():
                if sid not in hash_cache:
                    try:
                        full = await ra.get_game_hashes_full(sid)
                        hash_cache[sid] = [h.get("MD5", "").lower() for h in full if h.get("MD5")]
                    except Exception as exc:
                        applog.warning("system", f"Subset cache: hashes failed for subset {sid}: {exc}")
                        hash_cache[sid] = []
                for md5 in hash_cache[sid]:
                    rows.append((parent, sid, stitle, cid, md5))

        with Session(engine) as s:
            if game_ids is None:
                s.exec(text("DELETE FROM ra_subset_hash"))
            else:
                for parent in fetch:
                    s.exec(text(f"DELETE FROM ra_subset_hash WHERE parent_game_id = {int(parent)}"))
            for (parent, sid, stitle, cid, md5) in rows:
                s.add(RASubsetHash(parent_game_id=parent, subset_game_id=sid,
                                   subset_title=stitle, console_id=cid, md5=md5))
            app_settings.set(s, "subset_cache_last_sync", datetime.utcnow().isoformat())
            s.commit()
    finally:
        activity_store.finish("subset-sync")

    n_subsets = len({sid for ent in fetch.values() for sid in ent["subsets"]})
    applog.info("system", "Subset cache refreshed",
                {"parents": len(fetch), "subsets": n_subsets, "hashes": len(rows), "scoped": game_ids is not None})

    with Session(engine) as s:
        flags = recompute_subset_flags(s)
    return {"status": "ok", "parents": len(fetch), "subsets": n_subsets, "hashes": len(rows), **flags}


def recompute_subset_flags(session: Session) -> dict:
    """LOCAL full rebuild (no RA calls): from the cached subset hashes + the dashboard
    mirror, set each LibraryEntry.is_subset_rom and subset_info (hash-compatible subsets,
    each flagged mastered/not)."""
    by_md5: dict[str, list[tuple[int, str]]] = {}
    for r in session.exec(select(RASubsetHash)).all():
        if r.md5:
            by_md5.setdefault(r.md5.lower(), []).append((r.subset_game_id, r.subset_title))
    award_by_game = {p.game_id: p.highest_award_kind for p in session.exec(select(RAGameProgress)).all()}

    n_compat = n_avail = 0
    for e in session.exec(select(LibraryEntry)).all():
        new_subset = _is_subset(e)
        info: list[dict] = []
        if e.file_hash:
            seen: set[int] = set()
            for sid, stitle in by_md5.get(e.file_hash.lower(), []):
                if sid in seen:
                    continue
                seen.add(sid)
                info.append({"game_id": sid, "title": stitle,
                             "mastered": award_by_game.get(sid) == "mastered"})
        new_info = json.dumps(info) if info else ""
        if e.is_subset_rom != new_subset or e.subset_info != new_info:
            e.is_subset_rom = new_subset
            e.subset_info = new_info
            session.add(e)
        if info:
            n_compat += 1
            if any(not s["mastered"] for s in info):
                n_avail += 1
    session.commit()
    return {"subset_compatible": n_compat, "subset_available": n_avail}
