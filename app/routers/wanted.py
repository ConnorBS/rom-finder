from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select, func
from pathlib import Path
from datetime import datetime

from app.db.database import get_session, engine
from app.db.models import AppSetting, WantedGame, HuntStatus, HuntAttempt, LibraryEntry, RAGameProgress
from app.services import sources as source_registry
from app.services import activity as activity_store
from app.services.ra_client import SYSTEMS, RAClient
from app.services.title_utils import (
    search_title, search_variations, stem_from_rom_name, significant_terms, title_is_relevant,
)
from app.services import logger as applog

router = APIRouter(prefix="/wanted")
templates = Jinja2Templates(directory="app/templates")


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
    system_list = sorted({g.system for g in games if g.system})

    # Per-card hunt data
    hunting_ids = {
        int(t.task_id[len("hunt-"):])
        for t in activity_store.get_active()
        if t.task_id.startswith("hunt-") and not t.done
    }
    counts_raw = session.exec(
        select(HuntAttempt.wanted_game_id, func.count(HuntAttempt.id))
        .group_by(HuntAttempt.wanted_game_id)
    ).all()
    hunt_counts: dict[int, int] = dict(counts_raw)

    applog.log_navigation("wanted", {"game_count": len(games), "ra_configured": ra_configured})
    return templates.TemplateResponse(
        request, "wanted.html",
        {
            "games": games,
            "systems": sorted(SYSTEMS.items(), key=lambda x: x[1]),
            "ra_configured": ra_configured,
            "system_list": system_list,
            "hunting_ids": hunting_ids,
            "hunt_counts": hunt_counts,
        },
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
    # Already in library?
    in_library = session.exec(
        select(LibraryEntry).where(LibraryEntry.ra_game_id == ra_game_id)
    ).first()
    if in_library:
        return HTMLResponse(
            f'<span class="text-yellow-400 text-xs">Already in your library</span>'
            f'<a href="/collection" class="text-blue-400 text-xs hover:underline ml-2">View ↗</a>'
        )

    # Already in Wanted?
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

    background_tasks.add_task(_fetch_cover, game.id, ra_game_id, game_title, system)

    return templates.TemplateResponse(
        request, "partials/wanted_added.html",
        {"game": game},
    )


@router.post("/import-hub", response_class=HTMLResponse)
async def import_hub(request: Request, hub_ref: str = Form(...)):
    """PREVIEW an RA hub before importing: fetch every game (V2), annotate each LOCALLY
    with the configured user's progress (RA mirror) + owned/already-wanted state, and
    render a filterable, per-game deselectable list. The actual add is
    POST /wanted/import-hub/add — only the games the user keeps checked.
    Async + no Depends(get_session): fetch_hub_games does the paginated RA awaits, then
    annotation happens in a short session with no connection held across the await."""
    import base64
    import json as _json
    from app.services import hubs

    hub_id = hubs.parse_hub_ref(hub_ref)
    if not hub_id:
        return HTMLResponse('<span class="text-red-400 text-xs">Couldn\'t read a hub id from that.</span>')
    res = await hubs.fetch_hub_games(hub_id)
    if res.get("error") == "no_credentials":
        return HTMLResponse('<span class="text-yellow-500 text-xs">Add RA credentials in Settings first.</span>')
    if res.get("error"):
        return HTMLResponse(f'<span class="text-red-400 text-xs">Hub import failed: {res["error"]}</span>')
    games = res["games"]
    if not games:
        return HTMLResponse('<span class="text-gray-400 text-xs">No games found in that hub — check the id.</span>')

    with Session(engine) as session:
        wanted_ids = {w.ra_game_id for w in session.exec(select(WantedGame)).all()}
        owned_ids = {e.ra_game_id for e in session.exec(
            select(LibraryEntry).where(LibraryEntry.ra_game_id != None)  # noqa: E711
        ).all() if e.ra_game_id}
        prog = {p.game_id: p for p in session.exec(select(RAGameProgress)).all()}

    rows = []
    n_ach = n_owned = n_wanted = 0
    for g in games:
        gid = g["game_id"]
        if not gid:
            continue
        p = prog.get(gid)
        award = p.highest_award_kind if p else ""
        num_hc = p.num_awarded_hardcore if p else 0
        owned = gid in owned_ids
        wanted = gid in wanted_ids
        if g["achievements"] > 0:
            n_ach += 1
        if owned:
            n_owned += 1
        if wanted:
            n_wanted += 1
        # Carry the data needed to create the WantedGame in the checkbox value so the
        # add step needs no second RA fetch; base64(JSON) dodges any title delimiter.
        token = base64.b64encode(_json.dumps(
            {"i": gid, "t": g["title"], "c": g["console"]}).encode()).decode()
        rows.append({
            "game_id": gid, "title": g["title"], "console": g["console"],
            "achievements": g["achievements"], "points": g["points"],
            "award": award, "bucket": hubs.progress_bucket(award, num_hc),
            "owned": owned, "wanted": wanted, "token": token,
        })

    # Default-selected = not owned, not already wanted (the JS then applies the
    # has-achievements default on top). Seed the count so it reads sensibly even before
    # the init script runs.
    n_default = sum(1 for r in rows if not r["owned"] and not r["wanted"])
    return templates.TemplateResponse(
        request, "partials/hub_preview.html",
        {"hub_id": hub_id, "rows": rows, "total": len(rows),
         "n_ach": n_ach, "n_owned": n_owned, "n_wanted": n_wanted, "n_default": n_default},
    )


@router.post("/import-hub/add", response_class=HTMLResponse)
async def import_hub_add(
    games: list[str] = Form(default=[]),
    hub_id: int = Form(default=0),
    session: Session = Depends(get_session),
):
    """Add the games the user kept selected in the hub preview. `games` is a list of the
    preview's base64(JSON {i,t,c}) tokens (only checked checkboxes are posted). Re-checks
    owned/already-wanted server-side. Covers are NOT auto-fetched (a hub can be hundreds
    of games) — run the collection's Fetch-covers action."""
    import base64
    import json as _json
    from app.services.title_utils import clean_title, canonical_system

    if not games:
        return HTMLResponse('<span class="text-gray-400 text-xs">No games selected.</span>')

    existing_wanted = {w.ra_game_id for w in session.exec(select(WantedGame)).all()}
    owned = {e.ra_game_id for e in session.exec(
        select(LibraryEntry).where(LibraryEntry.ra_game_id != None)  # noqa: E711
    ).all() if e.ra_game_id}

    added = skipped_existing = skipped_owned = 0
    for tok in games:
        try:
            g = _json.loads(base64.b64decode(tok))
            gid = int(g["i"])
        except Exception:
            continue
        if not gid:
            continue
        if gid in existing_wanted:
            skipped_existing += 1
            continue
        if gid in owned:
            skipped_owned += 1
            continue
        system = canonical_system(g.get("c", ""), None) or g.get("c", "")
        session.add(WantedGame(game_title=clean_title(g.get("t", "")), system=system, ra_game_id=gid))
        existing_wanted.add(gid)
        added += 1
    session.commit()

    applog.log_action("import_hub", {"hub_id": hub_id, "added": added,
                                     "skipped_existing": skipped_existing, "skipped_owned": skipped_owned})
    return HTMLResponse(
        f'<span class="text-green-400 text-xs">✓ Added {added} selected game(s) to Wanted'
        + (f' — {skipped_owned} already owned' if skipped_owned else '')
        + (f', {skipped_existing} already wanted' if skipped_existing else '')
        + '.</span><a href="/wanted" class="text-blue-400 text-xs hover:underline ml-2">Reload ↗</a>'
    )


@router.post("/{wanted_id}/refresh-cover", response_class=HTMLResponse)
async def refresh_wanted_cover(
    wanted_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Delete existing cover and re-fetch from enabled sources."""
    if _get_setting(session, "covers_dir_readonly", "false") == "true":
        return HTMLResponse(
            '<button disabled class="absolute bottom-2 left-2 bg-red-900/50 border border-red-800 '
            'rounded-full px-1.5 py-0.5 text-xs text-red-300" title="Covers directory is read-only">Read-only</button>'
        )
    game = session.get(WantedGame, wanted_id)
    if not game:
        return HTMLResponse("")

    if game.cover_path:
        covers_dir = Path(_get_setting(session, "covers_dir", "static/covers"))
        cover_file = covers_dir / Path(game.cover_path).name
        cover_file.unlink(missing_ok=True)
        game.cover_path = ""
        game.updated_at = datetime.utcnow()
        session.add(game)
        session.commit()

    background_tasks.add_task(_fetch_cover, wanted_id, game.ra_game_id, game.game_title, game.system)
    applog.log_action("refresh_cover_wanted", {"id": wanted_id, "game": game.game_title})
    return HTMLResponse(
        '<button disabled class="absolute bottom-2 left-2 bg-blue-900/50 border border-blue-800 '
        'rounded-full px-1.5 py-0.5 text-xs text-blue-300">Fetching…</button>'
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
):
    """Return the source-search panel. Fetches RA hashes once, then renders one
    auto-loading section per enabled source so results trickle in in parallel."""
    # Read the wanted game, RA creds, and enabled sources in a short session and
    # release it before the RA hash fetch — never hold a connection across an await.
    with Session(engine) as session:
        wanted = session.get(WantedGame, game_id)
        if not wanted:
            return HTMLResponse('<p class="text-red-400 text-xs">Not found.</p>')
        ra = _get_ra_client(session)
        enabled_srcs = source_registry.enabled_sources(_enabled_source_ids(session))

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
):
    """HTMX: search a single source for a wanted game. Fires in parallel for
    each source section via hx-trigger='load'."""
    # Read the wanted game in a short session and release it before the source
    # search below — these sections fire in parallel per source, so holding a
    # connection across each search await is exactly what starved the pool.
    with Session(engine) as session:
        wanted = session.get(WantedGame, game_id)
    src = source_registry.get(source_id)
    results: list[dict] = []
    error: str | None = None

    if src is None:
        error = f"Unknown source: {source_id}"
    else:
        query_list = [q for q in queries.split("|") if q]
        seen_ids: set[str] = set()
        # Only show results that actually name the wanted game, so the panel
        # matches what the auto-hunt would accept ("search == hunt") — a loose
        # site search surfaces sibling titles (a different 'Pajama Sam' game).
        # Use the SEARCH title (drops the platform suffix AND "[Subset …]" tag) so a
        # game like "Ristar (Genesis/Mega Drive)" matches a plain "Ristar" result and
        # a subset matches its base-game ROM.
        want_terms = significant_terms(search_title(wanted.game_title)) if wanted else set()

        for query in query_list:
            try:
                src_results = await src.search(query, system)
            except Exception as exc:
                error = str(exc)
                break
            for r in src_results:
                uid = r.get("identifier", r.get("title", ""))
                if uid in seen_ids:
                    continue
                if want_terms and not title_is_relevant(r.get("title") or uid, want_terms):
                    continue  # sibling/unrelated game — drop it
                seen_ids.add(uid)
                results.append(r)
            if results:
                break  # stop on the first query that yields a relevant result

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
# Auto-hunt
# ---------------------------------------------------------------------------

def _is_hunting(game_id: int) -> bool:
    """True if an auto-hunt background task is currently active for this game."""
    task_id = f"hunt-{game_id}"
    return any(t.task_id == task_id and not t.done for t in activity_store.get_active())


def _hunt_attempt_count(session: Session, game_id: int) -> int:
    return session.exec(
        select(func.count(HuntAttempt.id)).where(HuntAttempt.wanted_game_id == game_id)
    ).one()


@router.post("/{game_id}/auto-hunt", response_class=HTMLResponse)
async def start_auto_hunt(
    request: Request,
    background_tasks: BackgroundTasks,
    game_id: int,
    session: Session = Depends(get_session),
):
    """Start (or restart) an auto-hunt for the given wanted game.

    On retry (exhausted games): clears failed HuntAttempt records so all
    sources are tried again. Already-verified attempts are preserved.
    """
    from app.services.hunter import auto_hunt

    game = session.get(WantedGame, game_id)
    if not game:
        return HTMLResponse("")

    # Reset exhausted/verified → hunting and clear previous failures so sources
    # are retried. (Re-hunting a 'verified' game lets the user recover from a bad
    # verification — e.g. a wrong-game match that left the slot stuck verified.)
    if game.status in (HuntStatus.exhausted, HuntStatus.verified):
        failed = session.exec(
            select(HuntAttempt)
            .where(HuntAttempt.wanted_game_id == game_id)
            .where(HuntAttempt.result != "verified")
        ).all()
        for a in failed:
            session.delete(a)
        game.status = HuntStatus.hunting
        session.add(game)
        session.commit()
        session.refresh(game)

    if not _is_hunting(game_id):
        background_tasks.add_task(auto_hunt, game_id)

    applog.log_action("auto_hunt_started", {
        "game": game.game_title, "system": game.system, "id": game_id,
    })
    return templates.TemplateResponse(
        request, "partials/wanted_card.html",
        {"game": game, "is_hunting": True, "hunt_attempts": 0},
    )


@router.get("/{game_id}/hunt-status", response_class=HTMLResponse)
async def hunt_status(
    request: Request,
    game_id: int,
    session: Session = Depends(get_session),
):
    """Polled by the wanted card while a hunt is active. Returns the updated
    card HTML; when the hunt finishes the returned card has no polling
    attributes so the polling stops automatically."""
    game = session.get(WantedGame, game_id)
    if not game:
        return HTMLResponse("")

    is_active = _is_hunting(game_id)
    attempt_count = _hunt_attempt_count(session, game_id)
    return templates.TemplateResponse(
        request, "partials/wanted_card.html",
        {"game": game, "is_hunting": is_active, "hunt_attempts": attempt_count},
    )


@router.get("/{game_id}/detail", response_class=HTMLResponse)
async def wanted_detail(
    request: Request,
    game_id: int,
    session: Session = Depends(get_session),
):
    """Slide-over detail panel content for a WantedGame card."""
    game = session.get(WantedGame, game_id)
    if not game:
        return HTMLResponse('<p class="text-red-400 text-sm">Game not found.</p>')
    attempts = session.exec(
        select(HuntAttempt)
        .where(HuntAttempt.wanted_game_id == game_id)
        .order_by(HuntAttempt.tried_at.desc())
    ).all()
    return templates.TemplateResponse(
        request, "partials/wanted_detail.html",
        {"game": game, "attempts": attempts},
    )


@router.delete("/{game_id}/attempts", response_class=HTMLResponse)
async def clear_hunt_attempts(
    game_id: int,
    session: Session = Depends(get_session),
):
    """Clear all non-verified hunt attempts and reset status to hunting.
    Allows a full fresh retry of all sources without re-downloading verified ROMs."""
    failed = session.exec(
        select(HuntAttempt)
        .where(HuntAttempt.wanted_game_id == game_id)
        .where(HuntAttempt.result != "verified")
    ).all()
    for a in failed:
        session.delete(a)
    game = session.get(WantedGame, game_id)
    if game and game.status == HuntStatus.exhausted:
        game.status = HuntStatus.hunting
        session.add(game)
    session.commit()
    applog.log_action("clear_hunt_attempts", {"game_id": game_id, "cleared": len(failed)})
    return HTMLResponse("")


@router.get("/{game_id}/attempts", response_class=HTMLResponse)
async def hunt_attempts(
    request: Request,
    game_id: int,
    session: Session = Depends(get_session),
):
    """HTMX: load hunt attempt history for an exhausted game."""
    attempts = session.exec(
        select(HuntAttempt)
        .where(HuntAttempt.wanted_game_id == game_id)
        .order_by(HuntAttempt.tried_at)
    ).all()
    return templates.TemplateResponse(
        request, "partials/hunt_attempts.html",
        {"attempts": attempts},
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _fetch_cover(wanted_id: int, ra_game_id: int, game_title: str, system: str, batch_id: str = "") -> None:
    """Try each enabled cover source in priority order; save the first image found."""
    import json as _json
    from app.db.database import engine
    from sqlmodel import Session as SyncSession
    from app.db.models import AppSetting
    from app.services import cover_sources as cover_source_registry
    from app.services import activity as activity_store

    task_id = f"cover-{wanted_id}"
    activity_store.start(task_id, f"Cover art: {game_title}", task_type="cover")

    with SyncSession(engine) as s:
        def _gs(key: str, default: str = "") -> str:
            setting = s.get(AppSetting, key)
            return setting.value if setting else default

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
            cover_file = covers_dir / f"{ra_game_id}.png"
            cover_file.write_bytes(image_bytes)
            with SyncSession(engine) as session:
                game = session.get(WantedGame, wanted_id)
                if game:
                    game.cover_path = f"covers/{ra_game_id}.png"
                    game.updated_at = datetime.utcnow()
                    session.add(game)
                    session.commit()
    finally:
        activity_store.finish(task_id)
        if batch_id:
            activity_store.increment(batch_id)
