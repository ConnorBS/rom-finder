"""Last-resort torrent/usenet fallback for the auto-hunt.

When the direct HTTP sources fail to yield a verified dump, `submit_external` hands
the wanted game to a configured download-client integration (Prowlarr search →
qBittorrent / SABnzbd) and parks the WantedGame in `awaiting_external`. The scheduler
`poll_active` pass then watches each job to completion and, once done, ingests +
RA-verifies the file exactly like a normal download — keeping it only if it
hash-verifies to the wanted game.

RA discipline: `submit_external` makes NO RA calls (the hunt already fetched the
accepted hashes and passes them in); the poller makes at most one `lookup_hash` per
completed download. Prowlarr is the only new external API (one search per game).
"""
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import (
    AppSetting, Download, DownloadStatus, ExternalDownload, HuntAttempt,
    HuntStatus, LibraryEntry, WantedGame,
)
from app.services import logger as applog
from app.services import settings as app_settings
from app.services.download_clients import registry as client_registry
from app.services.download_clients import selection
from app.services.rahasher import ra_hash_or_fallback
from app.services.ra_client import DEFAULT_FOLDER_MAP, RAClient

_NON_TERMINAL = ("submitted", "metadata", "downloading", "verifying")


def _gs(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _enabled_client(session: Session):
    """First registered download client whose enable flag is set."""
    for c in client_registry.all_clients():
        if _gs(session, f"download_client_{c.client_id}_enabled", "false") == "true":
            return c
    return None


def _staging_dir(session: Session, system: str) -> Path:
    use_review = _gs(session, "use_review_dir", "true") == "true"
    base = _gs(session, "check_dir", "/rom-check") if use_review else _gs(session, "download_dir", "/roms")
    folder_map = app_settings.get_json(session, "folder_map", {})
    folder = folder_map.get(system) or DEFAULT_FOLDER_MAP.get(system, system)
    return Path(base) / folder


async def submit_external(wanted_id: int, ra_stems: set, title_terms: set,
                          ra_hashes: set, system: str, game_title: str) -> bool:
    """Search the configured client's indexers and submit the best acceptable
    release. Returns True if a job was submitted (WantedGame → awaiting_external)."""
    with Session(engine) as session:
        client = _enabled_client(session)
        if client is None or not getattr(client, "protocols", set()):
            return False

    # Build the search query: the cleaned base title (best single query — one call).
    from app.services.title_utils import search_title
    query = search_title(game_title) or game_title
    try:
        releases = await client.search(query, system)
    except Exception as exc:
        applog.warning("hunt", f"External search failed ({client.client_id}): {exc}", {"wanted_id": wanted_id})
        return False

    # Keep releases that plausibly name the game; drop usenet packs (can't trim).
    cands = []
    for r in releases:
        if not selection.release_is_relevant(r.get("title", ""), title_terms):
            continue
        if r.get("protocol") == "usenet" and selection.looks_like_pack(r.get("title", "")):
            continue
        cands.append(r)
    if not cands:
        applog.info("hunt", f"External: no acceptable release for {game_title}", {"wanted_id": wanted_id})
        return False
    # Prefer torrents with seeders, then larger/seedier first.
    cands.sort(key=lambda r: (r.get("protocol") == "torrent", r.get("seeders") or 0), reverse=True)
    release = cands[0]

    save_path = _gs_save_path(client)
    try:
        sub = await client.submit(release, save_path)
    except Exception as exc:
        applog.warning("hunt", f"External submit failed ({client.client_id}): {exc}", {"wanted_id": wanted_id})
        return False
    handle = sub.get("job_handle", "")

    with Session(engine) as session:
        # A Download row so the job shows a live progress card (driven by the poller).
        dl = Download(
            game_title=game_title, system=system, file_name=release.get("title", ""),
            file_path=None, source_url=release.get("download_url", ""),
            source_id=client.client_id, status=DownloadStatus.downloading, progress=0.0,
            ra_game_id=None, hunt_task_id=f"hunt-{wanted_id}",
        )
        session.add(dl)
        session.commit()
        session.refresh(dl)
        ext = ExternalDownload(
            wanted_game_id=wanted_id, download_id=dl.id, client_id=client.client_id,
            protocol=sub.get("protocol", ""), job_handle=handle,
            release_title=release.get("title", ""), indexer=release.get("indexer", ""),
            save_path=save_path,
            match_data=json.dumps({"ra_stems": sorted(ra_stems), "title_terms": sorted(title_terms),
                                   "accepted_md5s": sorted(ra_hashes)}),
            needs_file_selection=bool(sub.get("needs_file_selection")),
            status="submitted",
        )
        session.add(ext)
        g = session.get(WantedGame, wanted_id)
        if g:
            g.status = HuntStatus.awaiting_external
            g.last_hunt_at = datetime.utcnow()
            session.add(g)
        session.commit()
    applog.info("hunt", f"Submitted to {client.client_id} ({sub.get('protocol')}): {release.get('title','')}",
                {"wanted_id": wanted_id, "indexer": release.get("indexer", ""), "handle": handle})
    return True


def _gs_save_path(client) -> str:
    return getattr(client, "qbit_save_path", "") or ""


async def poll_active() -> dict:
    """One pass over all non-terminal external jobs: advance file-selection,
    update progress, ingest completed ones, fail stalled ones."""
    with Session(engine) as session:
        active = session.exec(
            select(ExternalDownload).where(ExternalDownload.status.in_(_NON_TERMINAL))
        ).all()
        active_ids = [e.id for e in active]
        stall_min = int(_gs(session, "external_download_stall_minutes", "120") or "120")
    counts = {"active": len(active_ids), "completed": 0, "failed": 0}
    for ext_id in active_ids:
        try:
            res = await _advance(ext_id, stall_min)
            if res == "verified":
                counts["completed"] += 1
            elif res == "failed":
                counts["failed"] += 1
        except Exception as exc:
            applog.error("hunt", f"External poll error (ext {ext_id}): {exc}")
    return counts


async def _advance(ext_id: int, stall_min: int) -> str:
    with Session(engine) as session:
        ext = session.get(ExternalDownload, ext_id)
        if not ext or ext.status not in _NON_TERMINAL:
            return ""
        client = client_registry.get(ext.client_id)
        if client is None:
            return ""
        handle, protocol = ext.job_handle, ext.protocol
        md = json.loads(ext.match_data or "{}")
        stale = datetime.utcnow() - (ext.updated_at or ext.created_at) > timedelta(minutes=stall_min)

    st = await client.status(handle, protocol)

    if st.get("failed"):
        return await _fail(ext_id,st.get("error", "download failed"), delete_files=False)

    # Torrent file-selection once metadata is present (trim packs / keep all discs).
    if protocol == "torrent" and ext.needs_file_selection and ext.status in ("submitted", "metadata"):
        files = await client.list_files(handle)
        if not files:
            _touch(ext_id, "metadata", st.get("progress", 0.0))
            if stale:
                return await _fail(ext_id,"metadata never arrived", delete_files=True)
            return ""
        cls = selection.classify_files(files, set(md.get("ra_stems", [])), set(md.get("title_terms", [])))
        if cls["kind"] == "none":
            return await _fail(ext_id,"no file in the release matches the game", delete_files=True)
        await client.set_wanted_files(handle, cls["keep_indices"])
        _set_targets(ext_id, cls["keep_names"])

    # Progress
    _touch(ext_id, "downloading", st.get("progress", 0.0))

    if st.get("completed"):
        return await _ingest(ext_id, st)

    if stale:
        return await _fail(ext_id,f"stalled > {stall_min} min", delete_files=True)
    return ""


async def _ingest(ext_id: int, st: dict) -> str:
    """Move the wanted file(s) into staging, hash, RA-verify, and promote the
    linked Download row on success."""
    with Session(engine) as session:
        ext = session.get(ExternalDownload, ext_id)
        if not ext:
            return ""
        md = json.loads(ext.match_data or "{}")
        targets = json.loads(ext.target_files or "[]")
        wanted_id = ext.wanted_game_id
        dl_id = ext.download_id
        g = session.get(WantedGame, wanted_id)
        game_title = g.game_title if g else ext.release_title
        system = g.system if g else ""
        ra_game_id = g.ra_game_id if g else None
        use_review = _gs(session, "use_review_dir", "true") == "true"
        ra_user = _gs(session, "ra_username"); ra_key = _gs(session, "ra_api_key")
        stage_dir = _staging_dir(session, system)
        client_save_path = ext.save_path           # read before commit (commit expires attrs)
        ext.status = "verifying"
        ext.updated_at = datetime.utcnow()
        session.add(ext)
        session.commit()

    content_path = Path(st.get("content_path") or st.get("save_path") or "")
    rom_path = _locate_and_stage(content_path, client_save_path, targets, stage_dir)
    if rom_path is None:
        return await _fail(ext_id,f"completed file not found/accessible at {content_path}", delete_files=False)

    try:
        file_hash, _ = await ra_hash_or_fallback(rom_path, system)
    except Exception as exc:
        return await _fail(ext_id,f"hash failed: {exc}", delete_files=False)

    matched_id = None
    if ra_user and ra_key:
        try:
            match = await RAClient(ra_user, ra_key).lookup_hash(file_hash)
            matched_id = match.get("ID") if match else None
        except Exception as exc:
            applog.warning("hunt", f"External verify lookup failed: {exc}", {"wanted_id": wanted_id})

    from app.services.hunter import _verified_game_id
    accepted = set(md.get("accepted_md5s", []))
    verified_id = _verified_game_id(matched_id, ra_game_id, file_hash, accepted)

    with Session(engine) as session:
        ext = session.get(ExternalDownload, ext_id)
        if verified_id is None:
            applog.info("hunt", f"External: bad hash for {game_title} [{file_hash}]", {"wanted_id": wanted_id})
            try:
                rom_path.unlink(missing_ok=True)
            except OSError:
                pass
            return await _fail(ext_id,"downloaded dump did not hash-verify", delete_files=True)
        dl_status = DownloadStatus.pending_approval if use_review else DownloadStatus.completed
        d = session.get(Download, dl_id) if dl_id else None
        if d is None:
            d = Download(source_id=ext.client_id, source_url="")
            session.add(d)
        d.game_title = game_title; d.system = system
        d.file_name = rom_path.name; d.file_path = str(rom_path)
        d.status = dl_status; d.progress = 1.0
        d.file_hash = file_hash; d.hash_verified = True
        d.ra_game_id = verified_id; d.hunt_task_id = None
        d.updated_at = datetime.utcnow()
        session.add(d)
        session.add(HuntAttempt(
            wanted_game_id=wanted_id, source_id=ext.client_id, identifier=ext.indexer,
            file_name=rom_path.name, source_url="", file_hash=file_hash, result="verified",
        ))
        g = session.get(WantedGame, wanted_id)
        if g:
            g.status = HuntStatus.verified
            g.last_hunt_at = datetime.utcnow()
            session.add(g)
        if not use_review:
            session.add(LibraryEntry(
                game_title=game_title, system=system, file_name=rom_path.name,
                file_path=str(rom_path), file_hash=file_hash, hash_verified=True,
                ra_game_id=verified_id, ra_matched=True,
            ))
        ext.status = "verified"
        ext.progress = 1.0
        ext.updated_at = datetime.utcnow()
        session.add(ext)
        session.commit()
    applog.info("hunt", f"External verified: {rom_path.name} [{file_hash}]", {"wanted_id": wanted_id})
    return "verified"


def _locate_and_stage(content_path: Path, save_path: str, targets: list, stage_dir: Path) -> Path | None:
    """Find the wanted ROM file under the client's completed path and move it into
    rom-finder's staging dir. Requires the rom-finder container to be able to read
    the client's download folder (a shared mount)."""
    from app.routers.library import ROM_EXTENSIONS
    candidates: list[Path] = []
    roots = [p for p in (content_path, Path(save_path) if save_path else None) if p]
    for root in roots:
        try:
            if root.is_file():
                candidates.append(root)
            elif root.is_dir():
                candidates += [p for p in root.rglob("*") if p.is_file()]
        except OSError:
            continue
    if not candidates:
        return None
    # Prefer the selected target filenames; else any ROM-like file (largest).
    target_names = {Path(t).name for t in targets}
    roms = [p for p in candidates if p.suffix.lower() in ROM_EXTENSIONS]
    chosen_set = [p for p in roms if p.name in target_names] or roms
    if not chosen_set:
        return None
    # Disc descriptor preferred so its tracks resolve; else largest.
    chosen_set.sort(key=lambda p: (p.suffix.lower() in (".cue", ".gdi", ".m3u"), p.stat().st_size), reverse=True)
    primary = chosen_set[0]
    stage_dir.mkdir(parents=True, exist_ok=True)
    moved_primary = None
    # Move the primary + any same-stem siblings (multi-disc tracks) into staging.
    to_move = {primary} | {p for p in candidates if p.stem == primary.stem}
    for p in to_move:
        dest = stage_dir / p.name
        try:
            shutil.move(str(p), str(dest))
            if p == primary:
                moved_primary = dest
        except (OSError, shutil.Error) as exc:
            applog.warning("hunt", f"External stage move failed for {p.name}: {exc}")
    return moved_primary


def _touch(ext_id: int, status: str, progress: float) -> None:
    with Session(engine) as session:
        ext = session.get(ExternalDownload, ext_id)
        if not ext:
            return
        if ext.status in _NON_TERMINAL:
            ext.status = status
        ext.progress = progress
        ext.updated_at = datetime.utcnow()
        session.add(ext)
        if ext.download_id:
            d = session.get(Download, ext.download_id)
            if d and d.status == DownloadStatus.downloading:
                d.progress = progress
                d.updated_at = datetime.utcnow()
                session.add(d)
        session.commit()


def _set_targets(ext_id: int, names: list) -> None:
    with Session(engine) as session:
        ext = session.get(ExternalDownload, ext_id)
        if ext:
            ext.target_files = json.dumps(names)
            ext.updated_at = datetime.utcnow()
            session.add(ext)
            session.commit()


async def _fail(ext_id: int, reason: str, delete_files: bool) -> str:
    with Session(engine) as session:
        ext = session.get(ExternalDownload, ext_id)
        if not ext:
            return "failed"
        client = client_registry.get(ext.client_id)
        handle, protocol = ext.job_handle, ext.protocol
        wanted_id, dl_id = ext.wanted_game_id, ext.download_id
        ext.status = "failed"
        ext.error_message = reason[:300]
        ext.updated_at = datetime.utcnow()
        session.add(ext)
        if dl_id:
            d = session.get(Download, dl_id)
            if d:
                session.delete(d)
        g = session.get(WantedGame, wanted_id)
        if g and g.status == HuntStatus.awaiting_external:
            g.status = HuntStatus.exhausted
            g.last_hunt_at = datetime.utcnow()
            session.add(g)
        session.commit()
    applog.info("hunt", f"External job failed (wanted {wanted_id}): {reason}", {"wanted_id": wanted_id})
    if client is not None and handle:
        try:
            await client.cleanup(handle, protocol, delete_files)
        except Exception:
            pass
    return "failed"
