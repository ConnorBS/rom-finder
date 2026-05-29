"""Unified collection view — combines Library (owned ROMs) and Wanted (hunting list).

Status vocabulary:
  verified  — WantedGame.status == verified (RA hash confirmed)
  found     — has a LibraryEntry linked by ra_game_id or title+system, wanted entry unverified
  library   — LibraryEntry only, no corresponding WantedGame
  wanted    — WantedGame only, no LibraryEntry yet
"""
import json
import math
from datetime import datetime
from fastapi import APIRouter, Request, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

PER_PAGE_CARDS = 50
PER_PAGE_LIST = 100

from app.db.database import engine, get_session
from app.db.models import AppSetting, LibraryEntry, WantedGame, HuntStatus
from app.services import logger as applog
from app.services import cover_sources as cover_source_registry
from app.services import settings as app_settings
from app.services.ra_client import is_ra_unsupported, RA_UNSUPPORTED_SYSTEMS

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _build_collection(session: Session) -> list[dict]:
    library_entries = session.exec(select(LibraryEntry)).all()
    wanted_games = session.exec(select(WantedGame)).all()

    lib_by_ra: dict[int, LibraryEntry] = {}
    lib_by_key: dict[tuple, LibraryEntry] = {}
    for e in library_entries:
        if e.ra_game_id:
            lib_by_ra[e.ra_game_id] = e
        lib_by_key[(e.game_title.lower(), e.system.lower())] = e

    items: list[dict] = []
    seen_lib_ids: set[int] = set()

    for w in wanted_games:
        lib = lib_by_ra.get(w.ra_game_id) or lib_by_key.get((w.game_title.lower(), w.system.lower()))
        if w.status == HuntStatus.verified:
            status = "verified"
        elif lib:
            status = "found"
        else:
            status = "wanted"
        items.append({
            "game_title": w.game_title,
            "system": w.system,
            "status": status,
            "cover_path": w.cover_path or "",
            "file_hash": lib.file_hash if lib else None,
            "ra_matched": lib.ra_matched if lib else False,
            "ra_game_id": w.ra_game_id,
            "library_id": lib.id if lib else None,
            "wanted_id": w.id,
            "missing": lib.missing if lib else False,
            "duplicate": bool(lib and lib.duplicate_of),
            "duplicate_of": lib.duplicate_of if lib else None,
            "unsupported": is_ra_unsupported(w.system),
            "added_at": w.added_at,
        })
        if lib:
            seen_lib_ids.add(lib.id)

    for e in library_entries:
        if e.id not in seen_lib_ids:
            items.append({
                "game_title": e.game_title,
                "system": e.system,
                "status": "library",
                "cover_path": e.cover_path or "",
                "file_hash": e.file_hash,
                "ra_matched": e.ra_matched,
                "ra_game_id": e.ra_game_id,
                "library_id": e.id,
                "wanted_id": None,
                "missing": e.missing,
                "duplicate": bool(e.duplicate_of),
                "duplicate_of": e.duplicate_of,
                "unsupported": is_ra_unsupported(e.system),
                "added_at": e.added_at,
            })

    items.sort(key=lambda x: x["added_at"], reverse=True)
    return items


@router.get("/collection", response_class=HTMLResponse)
async def collection_page(
    request: Request,
    q: str = Query(default=""),
    system: str = Query(default=""),
    status: str = Query(default=""),
    view: str = Query(default="cards"),
    page: int = Query(default=1, ge=1),
    session: Session = Depends(get_session),
):
    all_items = _build_collection(session)
    systems = sorted({i["system"] for i in all_items if i["system"]})

    filtered = all_items
    if q:
        ql = q.lower()
        filtered = [i for i in filtered if ql in i["game_title"].lower()]
    if system:
        filtered = [i for i in filtered if i["system"] == system]
    if status == "no_ra":
        filtered = [i for i in filtered if i.get("file_hash") and not i.get("ra_matched") and not i.get("unsupported")]
    elif status == "unsupported":
        filtered = [i for i in filtered if i.get("unsupported")]
    elif status == "missing":
        filtered = [i for i in filtered if i.get("missing")]
    elif status == "duplicate":
        filtered = [i for i in filtered if i.get("duplicate")]
    elif status:
        filtered = [i for i in filtered if i["status"] == status]

    per_page = PER_PAGE_CARDS if view == "cards" else PER_PAGE_LIST
    total_filtered = len(filtered)
    total_pages = max(1, math.ceil(total_filtered / per_page))
    page = min(page, total_pages)
    start = (page - 1) * per_page
    items = filtered[start:start + per_page]

    # IDs for "hash filtered view" — capped at 500 to avoid oversized URLs
    all_filtered_lib_ids = [i["library_id"] for i in filtered if i.get("library_id")]
    if len(all_filtered_lib_ids) > 500:
        all_filtered_lib_ids = []

    applog.log_navigation("collection", {
        "total": len(all_items), "filtered": total_filtered,
        "q": q, "system": system, "status": status, "view": view, "page": page,
    })

    covers_enabled = any(
        session.get(AppSetting, f"cover_source_{src.source_id}_enabled") and
        session.get(AppSetting, f"cover_source_{src.source_id}_enabled").value == "true"
        for src in cover_source_registry.all_sources()
    )

    return templates.TemplateResponse(
        request, "collection.html",
        {
            "items": items,
            "systems": systems,
            "q": q,
            "selected_system": system,
            "selected_status": status,
            "view": view,
            "covers_enabled": covers_enabled,
            "page": page,
            "total_pages": total_pages,
            "total_filtered": total_filtered,
            "page_start": start + 1 if total_filtered else 0,
            "page_end": min(start + per_page, total_filtered),
            "all_filtered_lib_ids": all_filtered_lib_ids,
            "counts": {
                "total": len(all_items),
                "library": sum(1 for i in all_items if i["status"] == "library"),
                "wanted": sum(1 for i in all_items if i["status"] == "wanted"),
                "found": sum(1 for i in all_items if i["status"] == "found"),
                "verified": sum(1 for i in all_items if i["status"] == "verified"),
                "no_ra": sum(1 for i in all_items if i.get("file_hash") and not i.get("ra_matched") and not i.get("unsupported")),
                "unsupported": sum(1 for i in all_items if i.get("unsupported")),
                "duplicate": sum(1 for i in all_items if i.get("duplicate")),
            },
        },
    )


# ---------------------------------------------------------------------------
# Counts (polled by header bar)
# ---------------------------------------------------------------------------

@router.get("/collection/counts", response_class=HTMLResponse)
async def collection_counts(session: Session = Depends(get_session)):
    all_items = _build_collection(session)
    counts = {
        "total": len(all_items),
        "library": sum(1 for i in all_items if i["status"] == "library"),
        "wanted": sum(1 for i in all_items if i["status"] == "wanted"),
        "found": sum(1 for i in all_items if i["status"] == "found"),
        "verified": sum(1 for i in all_items if i["status"] == "verified"),
        "no_ra": sum(1 for i in all_items if i.get("file_hash") and not i.get("ra_matched") and not i.get("unsupported")),
        "unsupported": sum(1 for i in all_items if i.get("unsupported")),
        "duplicate": sum(1 for i in all_items if i.get("duplicate")),
    }
    parts = [f'<span>{counts["total"]} total</span>']
    if counts["verified"]:
        parts.append(f'<span class="text-green-500">{counts["verified"]} verified</span>')
    if counts["found"]:
        parts.append(f'<span class="text-blue-400">{counts["found"]} found</span>')
    if counts["library"]:
        parts.append(f'<span class="text-gray-400">{counts["library"]} library only</span>')
    if counts["wanted"]:
        parts.append(f'<span class="text-yellow-500">{counts["wanted"]} wanted</span>')
    if counts["no_ra"]:
        parts.append(f'<span class="text-orange-400">{counts["no_ra"]} no RA match</span>')
    if counts["unsupported"]:
        parts.append(f'<span class="text-slate-500">{counts["unsupported"]} unsupported</span>')
    if counts["duplicate"]:
        parts.append(f'<span class="text-purple-400">{counts["duplicate"]} duplicate</span>')
    return HTMLResponse(" ".join(parts))


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------

@router.post("/collection/recompute-duplicates", response_class=HTMLResponse)
async def recompute_duplicates_endpoint(session: Session = Depends(get_session)):
    """Re-derive the duplicate tags across the whole library (LOCAL — no RA calls)."""
    from app.services.duplicates import recompute_duplicates
    result = recompute_duplicates(session)
    applog.log_action("recompute_duplicates", result)
    n = result["duplicates"]
    msg = (f'&#10003; Tagged {n} duplicate{"s" if n != 1 else ""} '
           f'across {result["groups"]} group{"s" if result["groups"] != 1 else ""}.'
           if n else "&#10003; No duplicates found.")
    return HTMLResponse(f'<span class="text-green-400 text-xs">{msg}</span>')


@router.post("/collection/bulk/scan", response_class=HTMLResponse)
async def bulk_scan(session: Session = Depends(get_session)):
    """Scan the ROM directory and import untracked files into the library."""
    from app.routers.library import ROM_EXTENSIONS, _build_folder_to_system_map, _rom_title, is_disc_track

    download_dir = _get_setting(session, "download_dir", "")
    if not download_dir:
        return HTMLResponse('<span class="text-yellow-400 text-xs">No ROMs directory configured. Set it in Settings first.</span>')

    from pathlib import Path
    base = Path(download_dir)
    if not base.exists():
        return HTMLResponse(f'<span class="text-yellow-400 text-xs">Directory not found: {download_dir}</span>')

    folder_map = app_settings.get_json(session, "folder_map", {})
    folder_to_system = _build_folder_to_system_map(folder_map)
    existing_entries = session.exec(select(LibraryEntry)).all()
    existing_paths = {e.file_path for e in existing_entries}

    # --- Import new ROMs on disk -------------------------------------------
    cue_cache: dict[str, bool] = {}
    added = 0
    scanned = 0
    folders = 0
    for subdir in sorted(base.iterdir()):
        if not subdir.is_dir():
            continue
        folders += 1
        system = folder_to_system.get(subdir.name, subdir.name)
        for f in sorted(subdir.rglob('*')):
            if not f.is_file() or f.suffix.lower() not in ROM_EXTENSIONS:
                continue
            if is_disc_track(f, cue_cache):
                continue   # a .bin/.img track of a .cue disc — the .cue is the entry
            scanned += 1
            fp = str(f)
            if fp in existing_paths:
                continue
            session.add(LibraryEntry(
                game_title=_rom_title(f), system=system, file_name=f.name, file_path=fp,
            ))
            existing_paths.add(fp)
            added += 1

    # --- Clean up disc-track artifacts already in the library --------------
    # Tracks imported before this rule are real files on disk (so they'd never be
    # flagged "missing"), but they're not games — drop the unmatched ones so they
    # stop sitting in no_ra. (Never touch an RA-matched entry, just in case.)
    removed_tracks = 0
    track_ids: set[int] = set()
    for e in existing_entries:
        p = Path(e.file_path)
        if not e.ra_matched and p.exists() and is_disc_track(p, cue_cache):
            track_ids.add(e.id)
            session.delete(e)
            removed_tracks += 1
    live_entries = [e for e in existing_entries if e.id not in track_ids]

    # --- Reconcile against disk: flag missing / resurrect reappeared ------
    existing_entries = live_entries
    on_disk = {id(e): Path(e.file_path).exists() for e in existing_entries}
    restored = 0
    for e in existing_entries:
        if e.missing and on_disk[id(e)]:
            # ROM reappeared on disk → bring it back to life.
            e.missing = False
            e.missing_at = None
            session.add(e)
            restored += 1

    # Entries whose file is gone and not already flagged.
    gone = [e for e in existing_entries if not on_disk[id(e)] and not e.missing]
    flagged = 0
    present_count = sum(1 for e in existing_entries if not e.missing)
    if gone and len(gone) > max(5, present_count // 2):
        # More than half the present library looks missing — almost certainly an
        # unmounted share or wrong path. Don't flag en masse; warn instead.
        applog.warning(
            "library",
            f"Scan: {len(gone)} of {present_count} files missing — skipping "
            "missing-flag (possible unmounted drive). Check the ROMs directory.",
        )
    else:
        for e in gone:
            e.missing = True
            e.missing_at = datetime.utcnow()
            session.add(e)
            flagged += 1

    session.commit()
    if added or restored:
        from app.services.duplicates import recompute_duplicates
        recompute_duplicates(session)
    applog.log_action("bulk_scan", {
        "download_dir": download_dir, "scanned": scanned, "folders": folders,
        "added": added, "flagged_missing": flagged, "restored": restored,
        "removed_tracks": removed_tracks,
    })

    summary = f"Scanned {scanned:,} file{'s' if scanned != 1 else ''} across {folders} folder{'s' if folders != 1 else ''}"
    parts = []
    if added:
        parts.append(f"{added} imported")
    if flagged:
        parts.append(f"{flagged} marked missing")
    if restored:
        parts.append(f"{restored} restored")
    if removed_tracks:
        parts.append(f"{removed_tracks} disc-track artifacts removed")
    detail = " — " + ", ".join(parts) if parts else " — no changes"
    return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {summary}{detail}.</span>')


@router.post("/collection/library/{library_id}/delete", response_class=HTMLResponse)
async def delete_library_entry(library_id: int, session: Session = Depends(get_session)):
    """Permanently remove a library entry (the 'Delete' action on a missing ROM)."""
    e = session.get(LibraryEntry, library_id)
    if e:
        applog.log_action("library_delete", {
            "game": e.game_title, "system": e.system, "was_missing": e.missing,
        })
        session.delete(e)
        session.commit()
    return HTMLResponse("")


@router.post("/collection/library/{library_id}/to-wanted", response_class=HTMLResponse)
async def library_to_wanted(library_id: int, session: Session = Depends(get_session)):
    """Move a (missing) library entry to the Wanted hunt list, then remove it."""
    e = session.get(LibraryEntry, library_id)
    if not e:
        return HTMLResponse("")
    if not e.ra_game_id:
        return HTMLResponse(
            '<span class="text-yellow-400 text-xs">No RetroAchievements ID on this entry — can\'t add it to Wanted.</span>'
        )
    w = session.exec(select(WantedGame).where(WantedGame.ra_game_id == e.ra_game_id)).first()
    if w:
        w.status = HuntStatus.hunting
        w.updated_at = datetime.utcnow()
        session.add(w)
    else:
        session.add(WantedGame(
            game_title=e.game_title, system=e.system, ra_game_id=e.ra_game_id,
            cover_path=e.cover_path or "", status=HuntStatus.hunting,
        ))
    applog.log_action("library_to_wanted", {"game": e.game_title, "system": e.system})
    session.delete(e)
    session.commit()
    return HTMLResponse("")


@router.post("/collection/bulk/fetch-covers", response_class=HTMLResponse)
async def bulk_fetch_covers(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Queue cover fetches for all wanted games that have no cover yet."""
    if _get_setting(session, "covers_dir_readonly", "false") == "true":
        return HTMLResponse('<span class="text-red-400 text-xs">Covers directory is read-only. Disable it in Settings first.</span>')

    games_needing_cover = session.exec(
        select(WantedGame).where(WantedGame.cover_path == "")
    ).all()

    from app.routers.wanted import _fetch_cover
    queued = 0
    for game in games_needing_cover:
        queued += 1
    library_needing_cover = session.exec(
        select(LibraryEntry).where(LibraryEntry.cover_path == "")
    ).all()
    queued += len(library_needing_cover)

    if queued:
        from app.services import activity as activity_store
        activity_store.start_batch("cover-batch", "Fetching covers", queued, "cover")

    count = 0
    for game in games_needing_cover:
        background_tasks.add_task(_fetch_cover, game.id, game.ra_game_id, game.game_title, game.system, "cover-batch")
        count += 1
    for entry in library_needing_cover:
        background_tasks.add_task(_fetch_cover_for_library, entry.id, entry.ra_game_id, entry.game_title, entry.system, "cover-batch")

    applog.log_action("bulk_fetch_covers", {"queued": queued})
    if queued:
        return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; Fetching covers for {queued} game{"s" if queued != 1 else ""}…</span>')
    return HTMLResponse('<span class="text-gray-400 text-xs">All tracked games already have covers.</span>')


@router.post("/collection/bulk/rehash", response_class=HTMLResponse)
async def bulk_rehash(
    background_tasks: BackgroundTasks,
    library_ids: str = Query(default=""),
    unhashed_only: bool = Query(default=False),
    unmatched_only: bool = Query(default=False),
    session: Session = Depends(get_session),
):
    """Re-hash library entries (LOCAL — no RA calls). library_ids scopes to a
    subset; unhashed_only = only entries with no hash; unmatched_only = only
    not-yet-RA-matched entries (fixes disc/arcade/NDS that carry stale plain-MD5
    hashes, without disturbing already-matched cartridge games)."""
    stmt = select(LibraryEntry)
    if library_ids:
        ids = [int(x) for x in library_ids.split(",") if x.strip().isdigit()]
        stmt = stmt.where(LibraryEntry.id.in_(ids))
    if unhashed_only:
        stmt = stmt.where(LibraryEntry.file_hash.is_(None))
    if unmatched_only:
        stmt = stmt.where(LibraryEntry.ra_matched == False)  # noqa: E712
    entries = session.exec(stmt).all()

    if not entries:
        return HTMLResponse('<span class="text-gray-400 text-xs">No matching entries to hash.</span>')

    background_tasks.add_task(_do_rehash, [e.id for e in entries])
    applog.log_action("bulk_rehash", {"count": len(entries), "unhashed_only": unhashed_only, "unmatched_only": unmatched_only})
    label = "un-hashed" if unhashed_only else "unmatched" if unmatched_only else ""
    return HTMLResponse(f'<span class="text-blue-400 text-xs">&#8635; Hashing {len(entries)} {label} ROM{"s" if len(entries) != 1 else ""}… (local, no RA calls)</span>')


@router.post("/collection/bulk/verify", response_class=HTMLResponse)
async def bulk_verify(
    background_tasks: BackgroundTasks,
    library_ids: str = Query(default=""),          # scope to the current filtered view
    include_matched: bool = Query(default=False),  # re-verify already RA-matched too
    session: Session = Depends(get_session),
):
    """Verify library hashes against RetroAchievements.

    Scope: `library_ids` limits to the current filtered view (else the entire
    library). By default already-RA-matched ROMs are SKIPPED so we don't re-hammer
    RA for games already verified; `include_matched=true` re-verifies them too.
    """
    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if not username or not api_key:
        return HTMLResponse('<span class="text-yellow-400 text-xs">Add RetroAchievements credentials in Settings to verify hashes.</span>')

    stmt = select(LibraryEntry).where(
        LibraryEntry.file_hash.is_not(None),
        LibraryEntry.system.not_in(RA_UNSUPPORTED_SYSTEMS),  # never call RA for unverifiable platforms
    )
    if library_ids:
        ids = [int(x) for x in library_ids.split(",") if x.strip().isdigit()]
        stmt = stmt.where(LibraryEntry.id.in_(ids))
    if not include_matched:
        stmt = stmt.where(LibraryEntry.ra_matched == False)  # noqa: E712
    entries = session.exec(stmt).all()

    scope = "filtered view" if library_ids else "entire library"
    if not entries:
        none_msg = "Nothing to verify in this view." if library_ids else "All hashed ROMs are already verified."
        return HTMLResponse(f'<span class="text-gray-400 text-xs">{none_msg}</span>')

    background_tasks.add_task(_do_verify, [e.id for e in entries], username, api_key)
    applog.log_action("bulk_verify", {"count": len(entries), "scope": scope, "include_matched": include_matched})
    mode = "re-verifying incl. matched" if include_matched else "unmatched only"
    return HTMLResponse(
        f'<span class="text-blue-400 text-xs">&#10003; Checking {len(entries)} hash{"es" if len(entries) != 1 else ""} '
        f'against RetroAchievements ({scope}, {mode})…</span>'
    )


# ---------------------------------------------------------------------------
# Per-entry cover refresh
# ---------------------------------------------------------------------------

@router.post("/library/{library_id}/refresh-cover", response_class=HTMLResponse)
async def refresh_library_cover(
    library_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Delete existing cover for a library entry and re-fetch from enabled sources."""
    if _get_setting(session, "covers_dir_readonly", "false") == "true":
        return HTMLResponse(
            '<button disabled class="absolute bottom-2 left-2 bg-red-900/50 border border-red-800 '
            'rounded-full px-1.5 py-0.5 text-xs text-red-300" title="Covers directory is read-only">Read-only</button>'
        )
    from pathlib import Path
    entry = session.get(LibraryEntry, library_id)
    if not entry:
        return HTMLResponse("")

    if entry.cover_path:
        covers_dir = Path(_get_setting(session, "covers_dir", "static/covers"))
        cover_file = covers_dir / Path(entry.cover_path).name
        cover_file.unlink(missing_ok=True)
        entry.cover_path = ""
        session.add(entry)
        session.commit()

    background_tasks.add_task(
        _fetch_cover_for_library, library_id, entry.ra_game_id, entry.game_title, entry.system
    )
    applog.log_action("refresh_cover_library", {"id": library_id, "game": entry.game_title})
    return HTMLResponse(
        '<button disabled class="absolute bottom-2 left-2 bg-blue-900/50 border border-blue-800 '
        'rounded-full px-1.5 py-0.5 text-xs text-blue-300">Fetching…</button>'
    )


# ---------------------------------------------------------------------------
# Background task implementations
# ---------------------------------------------------------------------------

async def _do_rehash(entry_ids: list[int]) -> None:
    import asyncio
    from app.services.hasher import hash_rom
    from app.services.rahasher import ra_hash_or_fallback
    from app.services import activity as activity_store
    from pathlib import Path

    batch_id = "rehash-batch"
    activity_store.start_batch(
        batch_id,
        f"Hashing {len(entry_ids)} ROM{'s' if len(entry_ids) != 1 else ''}",
        len(entry_ids), "rehash", entry_ids=entry_ids,
    )

    loop = asyncio.get_event_loop()
    processed = 0
    with Session(engine) as session:
        for eid in entry_ids:
            if activity_store.is_cancelled(batch_id):
                break
            entry = session.get(LibraryEntry, eid)
            if not entry:
                activity_store.complete_entry(batch_id, eid)
                continue
            p = Path(entry.file_path)
            if not p.exists():
                activity_store.complete_entry(batch_id, eid)
                continue
            try:
                old_hash = entry.file_hash
                result, used_rahasher = await ra_hash_or_fallback(p, entry.system)
                applog.log_action("rehash_entry", {
                    "game": entry.game_title,
                    "system": entry.system,
                    "hasher": "rahasher" if used_rahasher else "python_md5",
                    "old_hash": old_hash or "none",
                    "new_hash": result,
                    "hash_changed": old_hash != result,
                })
                entry.file_hash = result
                entry.hashed_at = datetime.utcnow()
                entry.hash_verified = False
                entry.ra_matched = False
                entry.ra_checked_at = None   # hash changed → the old RA check is void; re-check it
                session.add(entry)
                processed += 1
            except Exception as exc:
                applog.warning("hash", f"Rehash failed for {entry.file_name}: {exc}")
            activity_store.complete_entry(batch_id, eid)
        session.commit()
    activity_store.finish(batch_id)
    applog.log_action("bulk_rehash_done", {"count": processed, "cancelled": activity_store.is_cancelled(batch_id)})
    _refresh_duplicates()


async def _fetch_cover_for_library(library_id: int, ra_game_id: int, game_title: str, system: str, batch_id: str = "") -> None:
    """Fetch cover art for a library-only entry (no WantedGame record)."""
    import json as _json
    from datetime import datetime as _dt
    from app.services import cover_sources as cover_source_registry
    from app.services import activity as activity_store
    from app.db.models import AppSetting

    task_id = f"cover-lib-{library_id}"
    activity_store.start(task_id, f"Cover art: {game_title}", task_type="cover")

    with Session(engine) as s:
        def _gs(key: str, default: str = "") -> str:
            setting = s.get(AppSetting, key)
            return setting.value if setting else default

        from pathlib import Path
        covers_dir = Path(_gs("covers_dir", "static/covers"))
        if _gs("covers_dir_readonly", "false") == "true":
            activity_store.finish(task_id)
            return

        config: dict = {
            "ra_username": _gs("ra_username"),
            "ra_api_key": _gs("ra_api_key"),
        }
        for src in cover_source_registry.all_sources():
            if src.requires_api_key:
                k = f"cover_source_{src.source_id}_api_key"
                config[k] = _gs(k)

        order_raw = _gs("cover_sources_order", "")
        all_srcs = cover_source_registry.all_sources()
        if order_raw:
            try:
                order = _json.loads(order_raw)
                src_map = {s.source_id: s for s in all_srcs}
                ordered = [src_map[sid] for sid in order if sid in src_map]
                ordered_ids = {s.source_id for s in ordered}
                ordered += [s for s in all_srcs if s.source_id not in ordered_ids]
            except (ValueError, KeyError):
                ordered = all_srcs
        else:
            ordered = all_srcs

        enabled_srcs = [
            s for s in ordered
            if _gs(f"cover_source_{s.source_id}_enabled", "false") == "true"
        ]

    covers_dir.mkdir(parents=True, exist_ok=True)
    cover_filename = f"{ra_game_id}.png" if ra_game_id else f"lib_{library_id}.png"
    cover_file = covers_dir / cover_filename

    # Reuse an already-downloaded cover without a network round-trip
    if cover_file.exists():
        with Session(engine) as session:
            entry = session.get(LibraryEntry, library_id)
            if entry:
                entry.cover_path = f"covers/{cover_filename}"
                session.add(entry)
                session.commit()
        activity_store.finish(task_id)
        if batch_id:
            activity_store.increment(batch_id)
        return

    image_bytes: bytes | None = None
    for src in enabled_srcs:
        try:
            image_bytes = await src.fetch_cover(ra_game_id, game_title, system, config)
            if image_bytes:
                break
        except Exception:
            continue

    try:
        if image_bytes:
            cover_file.write_bytes(image_bytes)
            with Session(engine) as session:
                entry = session.get(LibraryEntry, library_id)
                if entry:
                    entry.cover_path = f"covers/{cover_filename}"
                    session.add(entry)
                    session.commit()
    finally:
        activity_store.finish(task_id)
        if batch_id:
            activity_store.increment(batch_id)


async def _do_verify(entry_ids: list[int], username: str, api_key: str) -> None:
    from app.services.ra_client import RAClient
    from app.services import activity as activity_store
    from app.services.sources.errors import SourceRateLimitError

    batch_id = "verify-batch"
    activity_store.start_batch(
        batch_id,
        f"Verifying {len(entry_ids)} hash{'es' if len(entry_ids) != 1 else ''}",
        len(entry_ids), "verify", entry_ids=entry_ids,
    )

    ra = RAClient(username, api_key)
    matched = 0
    checked = 0

    # Fresh session per entry — never hold one open across the lookup await (SQLite
    # lock safety), and commit each result so progress is live + durable: no_ra drops
    # as we go, and a restart loses at most the in-flight entry, not the whole batch.
    for eid in entry_ids:
        if activity_store.is_cancelled(batch_id):
            break
        with Session(engine) as session:
            entry = session.get(LibraryEntry, eid)
            file_hash = entry.file_hash if entry else None
            file_name = entry.file_name if entry else str(eid)
        if not file_hash:
            activity_store.complete_entry(batch_id, eid)
            continue
        try:
            match = await ra.lookup_hash(file_hash)
        except SourceRateLimitError:
            # Stop rather than hammer RA — the rest is left to a later run or the
            # scheduled resumable re-verify.
            applog.warning("hash", "Bulk verify hit RA rate-limit (429) — stopping early to avoid hammering RA.")
            break
        except Exception as exc:
            applog.warning("hash", f"RA verify failed for {file_name}: {exc}")
            activity_store.complete_entry(batch_id, eid)
            continue
        checked += 1
        with Session(engine) as session:
            entry = session.get(LibraryEntry, eid)
            if entry:
                if match:
                    entry.ra_matched = True
                    entry.hash_verified = True
                    entry.ra_game_id = entry.ra_game_id or match.get("ID")
                    matched += 1
                entry.ra_checked_at = datetime.utcnow()  # leaves the resumable-verify work set
                session.add(entry)
                session.commit()
        activity_store.complete_entry(batch_id, eid)
    activity_store.finish(batch_id)
    applog.log_action("bulk_verify_done", {"checked": checked, "matched": matched})
    _refresh_duplicates()


def _refresh_duplicates() -> None:
    """Re-derive duplicate tags after a pass that may change hashes/RA ids. Best-effort
    — a tagging hiccup must never fail the hashing/verify batch that just succeeded."""
    try:
        from app.services.duplicates import recompute_duplicates
        with Session(engine) as session:
            recompute_duplicates(session)
    except Exception as exc:
        applog.warning("hash", f"recompute_duplicates failed: {exc}")
