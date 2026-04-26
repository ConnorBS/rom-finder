"""Unified collection view — combines Library (owned ROMs) and Wanted (hunting list).

Status vocabulary:
  verified  — WantedGame.status == verified (RA hash confirmed)
  found     — has a LibraryEntry linked by ra_game_id or title+system, wanted entry unverified
  library   — LibraryEntry only, no corresponding WantedGame
  wanted    — WantedGame only, no LibraryEntry yet
"""
import json
from datetime import datetime
from fastapi import APIRouter, Request, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.database import engine, get_session
from app.db.models import AppSetting, LibraryEntry, WantedGame, HuntStatus
from app.services import logger as applog
from app.services import cover_sources as cover_source_registry

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
    session: Session = Depends(get_session),
):
    all_items = _build_collection(session)
    systems = sorted({i["system"] for i in all_items if i["system"]})

    items = all_items
    if q:
        ql = q.lower()
        items = [i for i in items if ql in i["game_title"].lower()]
    if system:
        items = [i for i in items if i["system"] == system]
    if status == "no_ra":
        items = [i for i in items if i.get("file_hash") and not i.get("ra_matched")]
    elif status:
        items = [i for i in items if i["status"] == status]

    applog.log_navigation("collection", {
        "total": len(all_items), "filtered": len(items),
        "q": q, "system": system, "status": status, "view": view,
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
            "counts": {
                "total": len(all_items),
                "library": sum(1 for i in all_items if i["status"] == "library"),
                "wanted": sum(1 for i in all_items if i["status"] == "wanted"),
                "found": sum(1 for i in all_items if i["status"] == "found"),
                "verified": sum(1 for i in all_items if i["status"] == "verified"),
                "no_ra": sum(1 for i in all_items if i.get("file_hash") and not i.get("ra_matched")),
            },
        },
    )


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------

@router.post("/collection/bulk/scan", response_class=HTMLResponse)
async def bulk_scan(session: Session = Depends(get_session)):
    """Scan the ROM directory and import untracked files into the library."""
    from app.routers.library import ROM_EXTENSIONS, _build_folder_to_system_map, _rom_title

    download_dir = _get_setting(session, "download_dir", "")
    if not download_dir:
        return HTMLResponse('<span class="text-yellow-400 text-xs">No ROMs directory configured. Set it in Settings first.</span>')

    from pathlib import Path
    base = Path(download_dir)
    if not base.exists():
        return HTMLResponse(f'<span class="text-yellow-400 text-xs">Directory not found: {download_dir}</span>')

    folder_map = json.loads(_get_setting(session, "folder_map", "{}"))
    folder_to_system = _build_folder_to_system_map(folder_map)
    existing_paths = set(session.exec(select(LibraryEntry.file_path)).all())

    added = 0
    for subdir in sorted(base.iterdir()):
        if not subdir.is_dir():
            continue
        system = folder_to_system.get(subdir.name, subdir.name)
        for f in sorted(subdir.rglob('*')):
            if not f.is_file() or f.suffix.lower() not in ROM_EXTENSIONS:
                continue
            fp = str(f)
            if fp in existing_paths:
                continue
            session.add(LibraryEntry(
                game_title=_rom_title(f), system=system, file_name=f.name, file_path=fp,
            ))
            existing_paths.add(fp)
            added += 1

    session.commit()
    applog.log_action("bulk_scan", {"download_dir": download_dir, "added": added})

    if added:
        return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; Imported {added} ROM{"s" if added != 1 else ""} from disk.</span>')
    return HTMLResponse('<span class="text-gray-400 text-xs">Scan complete — no new ROMs found.</span>')


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
    session: Session = Depends(get_session),
):
    """Re-hash library entries. library_ids scopes to a subset; unhashed_only skips already-hashed."""
    stmt = select(LibraryEntry)
    if library_ids:
        ids = [int(x) for x in library_ids.split(",") if x.strip().isdigit()]
        stmt = stmt.where(LibraryEntry.id.in_(ids))
    if unhashed_only:
        stmt = stmt.where(LibraryEntry.file_hash.is_(None))
    entries = session.exec(stmt).all()

    if not entries:
        return HTMLResponse('<span class="text-gray-400 text-xs">No matching entries to hash.</span>')

    background_tasks.add_task(_do_rehash, [e.id for e in entries])
    applog.log_action("bulk_rehash", {"count": len(entries), "unhashed_only": unhashed_only})
    label = "un-hashed" if unhashed_only else ""
    return HTMLResponse(f'<span class="text-blue-400 text-xs">&#8635; Hashing {len(entries)} {label} ROM{"s" if len(entries) != 1 else ""}…</span>')


@router.post("/collection/bulk/verify", response_class=HTMLResponse)
async def bulk_verify(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Verify unmatched library entries against RetroAchievements."""
    username = _get_setting(session, "ra_username")
    api_key = _get_setting(session, "ra_api_key")
    if not username or not api_key:
        return HTMLResponse('<span class="text-yellow-400 text-xs">Add RetroAchievements credentials in Settings to verify hashes.</span>')

    unverified = session.exec(
        select(LibraryEntry).where(
            LibraryEntry.ra_matched == False,  # noqa: E712
            LibraryEntry.file_hash.is_not(None),
        )
    ).all()

    if not unverified:
        return HTMLResponse('<span class="text-gray-400 text-xs">All hashed ROMs are already verified.</span>')

    background_tasks.add_task(_do_verify, [e.id for e in unverified], username, api_key)
    applog.log_action("bulk_verify", {"count": len(unverified)})
    return HTMLResponse(f'<span class="text-blue-400 text-xs">&#10003; Checking {len(unverified)} hash{"es" if len(unverified) != 1 else ""} against RetroAchievements…</span>')


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
    from app.services.rahasher import compute_ra_hash
    from app.services import activity as activity_store
    from pathlib import Path

    batch_id = "rehash-batch"
    activity_store.start_batch(
        batch_id,
        f"Hashing {len(entry_ids)} ROM{'s' if len(entry_ids) != 1 else ''}",
        len(entry_ids), "rehash", entry_ids=entry_ids,
    )

    loop = asyncio.get_event_loop()
    with Session(engine) as session:
        for eid in entry_ids:
            entry = session.get(LibraryEntry, eid)
            if not entry:
                activity_store.increment(batch_id)
                continue
            p = Path(entry.file_path)
            if not p.exists():
                activity_store.increment(batch_id)
                continue
            try:
                result = await compute_ra_hash(p, entry.system)
                if result is None:
                    result = await loop.run_in_executor(None, hash_rom, p, entry.system)
                entry.file_hash = result
                entry.hashed_at = datetime.utcnow()
                entry.hash_verified = False
                entry.ra_matched = False
                session.add(entry)
            except Exception as exc:
                applog.warning("hash", f"Rehash failed for {entry.file_name}: {exc}")
            activity_store.increment(batch_id)
        session.commit()
    applog.log_action("bulk_rehash_done", {"count": len(entry_ids)})


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

    batch_id = "verify-batch"
    activity_store.start_batch(
        batch_id,
        f"Verifying {len(entry_ids)} hash{'es' if len(entry_ids) != 1 else ''}",
        len(entry_ids), "verify", entry_ids=entry_ids,
    )

    ra = RAClient(username, api_key)
    matched = 0

    with Session(engine) as session:
        for eid in entry_ids:
            entry = session.get(LibraryEntry, eid)
            if not entry or not entry.file_hash:
                activity_store.increment(batch_id)
                continue
            try:
                match = await ra.lookup_hash(entry.file_hash)
                if match:
                    entry.ra_matched = True
                    entry.hash_verified = True
                    entry.ra_game_id = entry.ra_game_id or match.get("ID")
                    matched += 1
                    session.add(entry)
            except Exception as exc:
                applog.warning("hash", f"RA verify failed for {entry.file_name}: {exc}")
            activity_store.increment(batch_id)
        session.commit()
    applog.log_action("bulk_verify_done", {"checked": len(entry_ids), "matched": matched})
