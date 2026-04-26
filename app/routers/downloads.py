import json
import shutil
from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from pathlib import Path
from datetime import datetime

from app.db.database import get_session
from app.db.models import Download, DownloadStatus, AppSetting, LibraryEntry, WantedGame, HuntStatus
from app.services import sources as source_registry
from app.services.hasher import hash_rom, extract_rom_from_zip, DISC_SYSTEMS
from app.services.rahasher import compute_ra_hash
from app.services.ra_client import DEFAULT_FOLDER_MAP
from app.services import logger as applog

router = APIRouter(prefix="/downloads")
templates = Jinja2Templates(directory="app/templates")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _resolve_folder(folder_map: dict, system: str) -> str:
    """Return the mapped folder name for a system.
    Priority: user folder_map → DEFAULT_FOLDER_MAP → system name as-is."""
    return folder_map.get(system) or DEFAULT_FOLDER_MAP.get(system, system)


@router.get("", response_class=HTMLResponse)
async def downloads_page(request: Request, session: Session = Depends(get_session)):
    all_downloads = session.exec(
        select(Download).order_by(Download.created_at.desc())
    ).all()
    pending = [d for d in all_downloads if d.status == DownloadStatus.pending_approval]
    active = [d for d in all_downloads if d.status != DownloadStatus.pending_approval]
    ra_configured = bool(_get_setting(session, "ra_username") and _get_setting(session, "ra_api_key"))
    applog.log_navigation("downloads", {"pending": len(pending), "active": len(active)})
    return templates.TemplateResponse(
        request, "downloads.html",
        {"pending": pending, "active": active, "ra_configured": ra_configured},
    )


@router.post("/start", response_class=HTMLResponse)
async def start_download(
    request: Request,
    background_tasks: BackgroundTasks,
    game_title: str = Form(...),
    system: str = Form(default=""),
    file_name: str = Form(...),
    archive_identifier: str = Form(...),
    source_id: str = Form(default="archive_org"),
    ra_game_id: int = Form(default=0),
    inline: str = Form(default="false"),
    session: Session = Depends(get_session),
):
    tag = "div" if inline == "true" else "li"
    use_review = _get_setting(session, "use_review_dir", "true") == "true"
    if use_review and _get_setting(session, "check_dir_readonly", "false") == "true":
        return HTMLResponse(
            f'<{tag} class="flex items-center px-4 py-2.5 bg-red-900/20 border-l-2 border-red-600 gap-4">'
            '<p class="text-red-400 text-xs">Review directory is read-only — disable the lock in Settings to download ROMs.</p>'
            f'</{tag}>'
        )
    if not use_review and _get_setting(session, "download_dir_readonly", "false") == "true":
        return HTMLResponse(
            f'<{tag} class="flex items-center px-4 py-2.5 bg-red-900/20 border-l-2 border-red-600 gap-4">'
            '<p class="text-red-400 text-xs">ROMs directory is read-only — disable the lock in Settings to download ROMs.</p>'
            f'</{tag}>'
        )
    src = source_registry.get(source_id) or source_registry.get("archive_org")
    source_url = src.get_download_url(archive_identifier, file_name)

    download = Download(
        game_title=game_title,
        system=system,
        file_name=file_name,
        source_url=source_url,
        source_id=source_id,
        archive_identifier=archive_identifier,
        status=DownloadStatus.pending,
        ra_game_id=ra_game_id if ra_game_id else None,
    )
    session.add(download)
    session.commit()
    session.refresh(download)

    background_tasks.add_task(_run_download, download.id)

    eid = f"queued-{download.id}"
    return HTMLResponse(
        f'<{tag} id="{eid}" class="flex items-center justify-between px-4 py-2.5 bg-green-900/20 border-l-2 border-green-600 gap-4">'
        f'<div class="min-w-0">'
        f'<p class="text-green-400 text-xs font-medium">Queued</p>'
        f'<p class="text-gray-500 text-xs font-mono truncate">{download.file_name}</p>'
        f'</div>'
        f'<a href="/downloads" class="text-blue-400 text-xs hover:underline flex-shrink-0">View queue</a>'
        f'<script>setTimeout(function(){{var e=document.getElementById("{eid}");if(e)e.remove();}},4000);</script>'
        f'</{tag}>'
    )


@router.get("/{download_id}/status", response_class=HTMLResponse)
async def download_status(
    request: Request,
    download_id: int,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if not download:
        return HTMLResponse('<p class="text-red-400 text-sm">Download not found.</p>')
    ra_configured = bool(_get_setting(session, "ra_username") and _get_setting(session, "ra_api_key"))
    return templates.TemplateResponse(request, "partials/download_item.html", {"download": download, "ra_configured": ra_configured})


@router.post("/{download_id}/approve", response_class=HTMLResponse)
async def approve_download(
    download_id: int,
    session: Session = Depends(get_session),
):
    if _get_setting(session, "download_dir_readonly", "false") == "true":
        return HTMLResponse(
            '<p class="text-red-400 text-xs px-4 py-2">ROMs directory is read-only — the app cannot make edits, deletes, or writes to it. Disable this in Settings to approve downloads.</p>'
        )
    download = session.get(Download, download_id)
    if not download or not download.file_path:
        return HTMLResponse("")

    download_dir = _get_setting(session, "download_dir", "/roms")
    folder_map = json.loads(_get_setting(session, "folder_map", "{}"))
    folder_name = _resolve_folder(folder_map, download.system)

    final_dir = Path(download_dir) / folder_name
    final_dir.mkdir(parents=True, exist_ok=True)

    src_path = Path(download.file_path)
    dest_path = final_dir / src_path.name
    if src_path.exists():
        shutil.move(str(src_path), str(dest_path))

    entry = LibraryEntry(
        game_title=download.game_title,
        system=download.system,
        file_name=dest_path.name,
        file_path=str(dest_path),
        file_hash=download.file_hash,
        hash_verified=download.hash_verified,
        ra_game_id=download.ra_game_id,
        ra_matched=download.hash_verified,
    )
    session.add(entry)

    # Mark matching wanted game verified
    if download.ra_game_id:
        wanted = session.exec(
            select(WantedGame).where(WantedGame.ra_game_id == download.ra_game_id)
        ).first()
        if wanted and wanted.status != HuntStatus.verified:
            wanted.status = HuntStatus.verified
            wanted.updated_at = datetime.utcnow()
            session.add(wanted)

    applog.log_action("approve_download", {
        "game": download.game_title, "file": dest_path.name,
        "system": download.system, "dest": str(dest_path),
        "ra_verified": download.hash_verified,
    })
    session.delete(download)
    session.commit()
    return HTMLResponse("")


@router.post("/{download_id}/reject", response_class=HTMLResponse)
async def reject_download(
    download_id: int,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if not download:
        return HTMLResponse("")
    applog.log_action("reject_download", {
        "game": download.game_title, "file": download.file_name, "system": download.system,
    })
    if download.file_path:
        p = Path(download.file_path)
        if p.exists():
            p.unlink()
    session.delete(download)
    session.commit()
    return HTMLResponse("")


@router.post("/approve-all", response_class=HTMLResponse)
async def approve_all_verified(
    request: Request,
    session: Session = Depends(get_session),
):
    """Approve all hash-verified pending items at once."""
    if _get_setting(session, "download_dir_readonly", "false") == "true":
        all_downloads = session.exec(select(Download).order_by(Download.created_at.desc())).all()
        pending_list = [d for d in all_downloads if d.status == DownloadStatus.pending_approval]
        active_list = [d for d in all_downloads if d.status != DownloadStatus.pending_approval]
        ra_configured = bool(_get_setting(session, "ra_username") and _get_setting(session, "ra_api_key"))
        return templates.TemplateResponse(
            request, "downloads.html",
            {"pending": pending_list, "active": active_list, "ra_configured": ra_configured, "readonly_error": True},
        )
    pending = session.exec(
        select(Download).where(Download.status == DownloadStatus.pending_approval)
    ).all()

    download_dir = _get_setting(session, "download_dir", "/roms")
    folder_map = json.loads(_get_setting(session, "folder_map", "{}"))

    for download in pending:
        if not download.file_path:
            continue
        folder_name = _resolve_folder(folder_map, download.system)
        final_dir = Path(download_dir) / folder_name
        final_dir.mkdir(parents=True, exist_ok=True)
        src_path = Path(download.file_path)
        dest_path = final_dir / src_path.name
        if src_path.exists():
            shutil.move(str(src_path), str(dest_path))
        entry = LibraryEntry(
            game_title=download.game_title,
            system=download.system,
            file_name=dest_path.name,
            file_path=str(dest_path),
            file_hash=download.file_hash,
            hash_verified=download.hash_verified,
            ra_game_id=download.ra_game_id,
            ra_matched=download.hash_verified,
        )
        session.add(entry)
        if download.ra_game_id:
            wanted = session.exec(
                select(WantedGame).where(WantedGame.ra_game_id == download.ra_game_id)
            ).first()
            if wanted and wanted.status != HuntStatus.verified:
                wanted.status = HuntStatus.verified
                wanted.updated_at = datetime.utcnow()
                session.add(wanted)
        session.delete(download)

    session.commit()

    applog.log_action("approve_all_downloads", {"approved_count": len(pending)})

    all_downloads = session.exec(select(Download).order_by(Download.created_at.desc())).all()
    pending_list = [d for d in all_downloads if d.status == DownloadStatus.pending_approval]
    active_list = [d for d in all_downloads if d.status != DownloadStatus.pending_approval]
    ra_configured = bool(_get_setting(session, "ra_username") and _get_setting(session, "ra_api_key"))
    return templates.TemplateResponse(
        request, "downloads.html",
        {"pending": pending_list, "active": active_list, "ra_configured": ra_configured},
    )


@router.delete("/{download_id}", response_class=HTMLResponse)
async def delete_download(
    download_id: int,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if download:
        applog.log_action_verbose("delete_download", {
            "id": download_id, "game": download.game_title, "status": download.status,
        })
        session.delete(download)
        session.commit()
    return HTMLResponse("")


@router.post("/{download_id}/hash", response_class=HTMLResponse)
async def hash_download(
    request: Request,
    download_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if not download or not download.file_path:
        return HTMLResponse("")
    if not Path(download.file_path).exists():
        return HTMLResponse(
            f'<div id="download-{download_id}" class="bg-gray-900 border border-red-900 rounded-lg p-4">'
            '<p class="text-red-400 text-xs">File not found on disk.</p></div>'
        )
    download.status = DownloadStatus.hashing
    download.updated_at = datetime.utcnow()
    session.add(download)
    session.commit()
    session.refresh(download)
    ra_configured = bool(_get_setting(session, "ra_username") and _get_setting(session, "ra_api_key"))
    background_tasks.add_task(_run_hash, download_id)
    return templates.TemplateResponse(request, "partials/download_item.html", {"download": download, "ra_configured": ra_configured})


@router.post("/{download_id}/verify-ra", response_class=HTMLResponse)
async def verify_ra_download(
    request: Request,
    download_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if not download or not download.file_hash:
        return HTMLResponse("")
    ra_username = _get_setting(session, "ra_username")
    ra_api_key = _get_setting(session, "ra_api_key")
    if not ra_username or not ra_api_key:
        return HTMLResponse(
            f'<div id="download-{download_id}" class="bg-gray-900 border border-yellow-900 rounded-lg p-4">'
            '<p class="text-yellow-400 text-xs">RetroAchievements credentials not configured — add them in Settings.</p></div>'
        )
    download.status = DownloadStatus.verifying
    download.updated_at = datetime.utcnow()
    session.add(download)
    session.commit()
    session.refresh(download)
    ra_configured = True
    background_tasks.add_task(_run_verify_ra, download_id)
    return templates.TemplateResponse(request, "partials/download_item.html", {"download": download, "ra_configured": ra_configured})


async def _run_hash(download_id: int) -> None:
    from app.db.database import engine
    from sqlmodel import Session

    with Session(engine) as session:
        download = session.get(Download, download_id)
        if not download or not download.file_path:
            return
        rom_path = Path(download.file_path)
        if not rom_path.exists():
            download.status = DownloadStatus.pending_approval
            download.updated_at = datetime.utcnow()
            session.add(download)
            session.commit()
            return
        try:
            ra_hash_result = await compute_ra_hash(rom_path, download.system)
            file_hash = ra_hash_result if ra_hash_result is not None else hash_rom(rom_path, download.system)
            download.file_hash = file_hash
            download.hash_verified = False
            applog.log_action("manual_hash", {"game": download.game_title, "file": rom_path.name, "hash": file_hash})
        except Exception as exc:
            applog.warning("hash", f"Manual hash failed: {exc}", {"download_id": download_id})
        download.status = DownloadStatus.pending_approval
        download.updated_at = datetime.utcnow()
        session.add(download)
        session.commit()


async def _run_verify_ra(download_id: int) -> None:
    from app.db.database import engine
    from sqlmodel import Session

    with Session(engine) as session:
        download = session.get(Download, download_id)
        if not download or not download.file_hash:
            return
        ra_username = _get_setting(session, "ra_username")
        ra_api_key = _get_setting(session, "ra_api_key")
        if not ra_username or not ra_api_key:
            applog.warning("hash", "Manual RA verify skipped: no credentials configured", {"download_id": download_id})
        else:
            from app.services.ra_client import RAClient
            ra = RAClient(ra_username, ra_api_key)
            try:
                match = await ra.lookup_hash(download.file_hash)
                if match:
                    download.hash_verified = True
                    download.ra_game_id = download.ra_game_id or match.get("ID")
                    applog.log_action("manual_verify_ra", {
                        "game": download.game_title, "hash": download.file_hash, "ra_game_id": download.ra_game_id,
                    })
                else:
                    applog.info("hash", f"Manual RA verify: no match for {download.game_title}", {
                        "hash": download.file_hash, "game": download.game_title,
                    })
            except Exception as exc:
                applog.warning("hash", f"Manual RA verify failed: {exc}", {"download_id": download_id, "hash": download.file_hash})
        download.status = DownloadStatus.pending_approval
        download.updated_at = datetime.utcnow()
        session.add(download)
        session.commit()


async def _run_download(download_id: int) -> None:
    from app.db.database import engine
    from sqlmodel import Session

    with Session(engine) as session:
        download = session.get(Download, download_id)
        if not download:
            return

        use_review = _get_setting(session, "use_review_dir", "true") == "true"
        check_dir = _get_setting(session, "check_dir", "/rom-check")
        download_dir = _get_setting(session, "download_dir", "/roms")
        folder_map = json.loads(_get_setting(session, "folder_map", "{}"))
        folder_name = _resolve_folder(folder_map, download.system)
        base_dir = check_dir if use_review else download_dir
        dest = Path(base_dir) / folder_name / download.file_name
        dest.parent.mkdir(parents=True, exist_ok=True)

        download.status = DownloadStatus.downloading
        download.updated_at = datetime.utcnow()
        session.add(download)
        session.commit()
        applog.log_download(download.game_title, download.file_name, download.source_url, "started")

        async def on_progress(fraction: float):
            with Session(engine) as s:
                d = s.get(Download, download_id)
                if d:
                    d.progress = fraction
                    d.updated_at = datetime.utcnow()
                    s.add(d)
                    s.commit()

        try:
            src = source_registry.get(download.source_id) or source_registry.get("archive_org")
            await src.download_file(download.source_url, dest, on_progress)

            # Transition to hashing so the UI can show a spinner while we compute
            download.status = DownloadStatus.hashing
            download.updated_at = datetime.utcnow()
            session.add(download)
            session.commit()
            session.refresh(download)

            rom_path = dest
            if dest.suffix.lower() in (".zip", ".7z"):
                import zipfile as _zf
                try:
                    rom_path = extract_rom_from_zip(dest)
                except _zf.BadZipFile:
                    # The downloaded file is not actually a ZIP (e.g. Vimm served
                    # a raw CHD/ISO). Strip the false extension and keep as-is.
                    real_path = dest.with_suffix("")
                    dest.rename(real_path)
                    rom_path = real_path

            # Compute RA hash — try RAHasher binary first, fall back to Python
            ra_hash_result = await compute_ra_hash(rom_path, download.system)
            if ra_hash_result is not None:
                file_hash = ra_hash_result
                hasher_used = "RAHasher"
            else:
                file_hash = hash_rom(rom_path, download.system)
                hasher_used = "Python"

            download.file_path = str(rom_path)
            download.file_name = rom_path.name
            download.file_hash = file_hash
            download.progress = 1.0

            # RA hash verification — runs regardless of ra_enabled so we always
            # know if a ROM is in the RA database; ra_enabled only gates whether
            # hash matching blocks/gates the approval flow in future.
            ra_username = _get_setting(session, "ra_username")
            ra_api_key = _get_setting(session, "ra_api_key")
            ra_matched = False
            ra_game_id_matched = None
            if not ra_username or not ra_api_key:
                applog.info("hash", f"RA verify skipped (no credentials): {rom_path.name}", {"hash": file_hash})
            else:
                download.status = DownloadStatus.verifying
                download.updated_at = datetime.utcnow()
                session.add(download)
                session.commit()

                from app.services.ra_client import RAClient
                ra = RAClient(ra_username, ra_api_key)
                try:
                    match = await ra.lookup_hash(file_hash)
                    if match:
                        download.hash_verified = True
                        download.ra_game_id = download.ra_game_id or match.get("ID")
                        ra_matched = True
                        ra_game_id_matched = match.get("ID")
                except Exception as exc:
                    applog.warning("hash", f"RA hash lookup failed: {exc}", {"file": rom_path.name, "hash": file_hash})

            applog.log_hash(rom_path.name, download.system, file_hash or "", hasher_used, ra_matched, ra_game_id_matched)
            applog.log_download(download.game_title, rom_path.name, download.source_url, "completed")

            if use_review:
                download.status = DownloadStatus.pending_approval
            else:
                download.status = DownloadStatus.completed
                entry = LibraryEntry(
                    game_title=download.game_title,
                    system=download.system,
                    file_name=rom_path.name,
                    file_path=str(rom_path),
                    file_hash=file_hash,
                    hash_verified=download.hash_verified,
                    ra_game_id=download.ra_game_id,
                    ra_matched=download.hash_verified,
                )
                session.add(entry)
                if download.ra_game_id:
                    wanted = session.exec(
                        select(WantedGame).where(WantedGame.ra_game_id == download.ra_game_id)
                    ).first()
                    if wanted and wanted.status != HuntStatus.verified:
                        wanted.status = HuntStatus.verified
                        wanted.updated_at = datetime.utcnow()
                        session.add(wanted)

        except Exception as exc:
            applog.log_download(download.game_title, download.file_name, download.source_url, "failed", str(exc))
            download.status = DownloadStatus.failed
            download.error_message = str(exc)

        download.updated_at = datetime.utcnow()
        session.add(download)
        session.commit()
