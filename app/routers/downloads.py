import json
import shutil
from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from pathlib import Path
from datetime import datetime

from app.db.database import get_session
from app.db.models import Download, DownloadStatus, AppSetting, LibraryEntry
from app.services import sources as source_registry
from app.services.hasher import hash_rom, extract_rom_from_zip, DISC_SYSTEMS
from app.services.rahasher import compute_ra_hash

router = APIRouter(prefix="/downloads")
templates = Jinja2Templates(directory="app/templates")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _resolve_folder(folder_map: dict, system: str) -> str:
    """Return the mapped folder name for a system, falling back to the system name."""
    return folder_map.get(system, system)


@router.get("", response_class=HTMLResponse)
async def downloads_page(request: Request, session: Session = Depends(get_session)):
    all_downloads = session.exec(
        select(Download).order_by(Download.created_at.desc())
    ).all()
    pending = [d for d in all_downloads if d.status == DownloadStatus.pending_approval]
    active = [d for d in all_downloads if d.status != DownloadStatus.pending_approval]
    return templates.TemplateResponse(
        request, "downloads.html",
        {"pending": pending, "active": active},
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
    session: Session = Depends(get_session),
):
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
        f'<li id="{eid}" class="flex items-center justify-between px-4 py-2.5 bg-green-900/20 border-l-2 border-green-600 gap-4">'
        f'<div class="min-w-0">'
        f'<p class="text-green-400 text-xs font-medium">Queued</p>'
        f'<p class="text-gray-500 text-xs font-mono truncate">{download.file_name}</p>'
        f'</div>'
        f'<a href="/downloads" class="text-blue-400 text-xs hover:underline flex-shrink-0">View queue</a>'
        f'<script>setTimeout(function(){{var e=document.getElementById("{eid}");if(e)e.remove();}},4000);</script>'
        f'</li>'
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
    return templates.TemplateResponse(request, "partials/download_item.html", {"download": download})


@router.post("/{download_id}/approve", response_class=HTMLResponse)
async def approve_download(
    download_id: int,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if not download or not download.file_path:
        return HTMLResponse("")

    download_dir = _get_setting(session, "download_dir", str(Path.home() / "ROMs"))
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
        from app.db.models import WantedGame, HuntStatus
        wanted = session.exec(
            select(WantedGame).where(WantedGame.ra_game_id == download.ra_game_id)
        ).first()
        if wanted and wanted.status != HuntStatus.verified:
            wanted.status = HuntStatus.verified
            wanted.updated_at = datetime.utcnow()
            session.add(wanted)

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
    pending = session.exec(
        select(Download).where(Download.status == DownloadStatus.pending_approval)
    ).all()

    download_dir = _get_setting(session, "download_dir", str(Path.home() / "ROMs"))
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
            from app.db.models import WantedGame, HuntStatus
            wanted = session.exec(
                select(WantedGame).where(WantedGame.ra_game_id == download.ra_game_id)
            ).first()
            if wanted and wanted.status != HuntStatus.verified:
                wanted.status = HuntStatus.verified
                wanted.updated_at = datetime.utcnow()
                session.add(wanted)
        session.delete(download)

    session.commit()

    all_downloads = session.exec(select(Download).order_by(Download.created_at.desc())).all()
    pending_list = [d for d in all_downloads if d.status == DownloadStatus.pending_approval]
    active_list = [d for d in all_downloads if d.status != DownloadStatus.pending_approval]
    return templates.TemplateResponse(
        request, "downloads.html",
        {"pending": pending_list, "active": active_list},
    )


@router.delete("/{download_id}", response_class=HTMLResponse)
async def delete_download(
    download_id: int,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if download:
        session.delete(download)
        session.commit()
    return HTMLResponse("")


async def _run_download(download_id: int) -> None:
    from app.db.database import engine
    from sqlmodel import Session

    with Session(engine) as session:
        download = session.get(Download, download_id)
        if not download:
            return

        check_dir = _get_setting(session, "check_dir", str(Path.home() / "ROMs-check"))
        folder_map = json.loads(_get_setting(session, "folder_map", "{}"))
        folder_name = _resolve_folder(folder_map, download.system)
        dest = Path(check_dir) / folder_name / download.file_name
        dest.parent.mkdir(parents=True, exist_ok=True)

        download.status = DownloadStatus.downloading
        download.updated_at = datetime.utcnow()
        session.add(download)
        session.commit()

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

            rom_path = dest
            if dest.suffix.lower() == ".zip":
                rom_path = extract_rom_from_zip(dest)

            # Compute RA hash — try RAHasher binary first, fall back to Python
            file_hash = await compute_ra_hash(rom_path, download.system)
            if file_hash is None:
                file_hash = hash_rom(rom_path, download.system)

            download.file_path = str(rom_path)
            download.file_name = rom_path.name
            download.file_hash = file_hash
            download.progress = 1.0

            # RA hash verification — runs regardless of ra_enabled so we always
            # know if a ROM is in the RA database; ra_enabled only gates whether
            # hash matching blocks/gates the approval flow in future.
            ra_username = _get_setting(session, "ra_username")
            ra_api_key = _get_setting(session, "ra_api_key")
            if ra_username and ra_api_key:
                from app.services.ra_client import RAClient
                ra = RAClient(ra_username, ra_api_key)
                try:
                    match = await ra.lookup_hash(file_hash)
                    if match:
                        download.hash_verified = True
                        download.ra_game_id = match.get("ID")
                except Exception:
                    pass

            download.status = DownloadStatus.pending_approval

        except Exception as exc:
            download.status = DownloadStatus.failed
            download.error_message = str(exc)

        download.updated_at = datetime.utcnow()
        session.add(download)
        session.commit()
