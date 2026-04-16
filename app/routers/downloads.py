from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from pathlib import Path
from datetime import datetime

from app.db.database import get_session
from app.db.models import Download, DownloadStatus, AppSetting
from app.services.archive_client import ArchiveClient
from app.services.hasher import hash_rom, extract_rom_from_zip

router = APIRouter(prefix="/downloads")
templates = Jinja2Templates(directory="app/templates")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


@router.get("", response_class=HTMLResponse)
async def downloads_page(request: Request, session: Session = Depends(get_session)):
    downloads = session.exec(
        select(Download).order_by(Download.created_at.desc())
    ).all()
    return templates.TemplateResponse(request, "downloads.html", {"downloads": downloads})


@router.post("/start", response_class=HTMLResponse)
async def start_download(
    request: Request,
    background_tasks: BackgroundTasks,
    game_title: str = Form(...),
    system: str = Form(default=""),
    file_name: str = Form(...),
    archive_identifier: str = Form(...),
    session: Session = Depends(get_session),
):
    """Enqueue a download and kick off the background task."""
    client = ArchiveClient()
    source_url = client.get_download_url(archive_identifier, file_name)

    download = Download(
        game_title=game_title,
        system=system,
        file_name=file_name,
        source_url=source_url,
        archive_identifier=archive_identifier,
        status=DownloadStatus.pending,
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
    """HTMX polling endpoint: returns current state of one download."""
    download = session.get(Download, download_id)
    if not download:
        return HTMLResponse('<p class="text-red-400 text-sm">Download not found.</p>')

    return templates.TemplateResponse(request, "partials/download_item.html", {"download": download})


@router.delete("/{download_id}", response_class=HTMLResponse)
async def delete_download(
    download_id: int,
    session: Session = Depends(get_session),
):
    download = session.get(Download, download_id)
    if download:
        session.delete(download)
        session.commit()
    return HTMLResponse("")  # HTMX swaps in empty string to remove the element


async def _run_download(download_id: int) -> None:
    """Background task: download the file and update DB progress."""
    from app.db.database import engine
    from sqlmodel import Session

    with Session(engine) as session:
        download = session.get(Download, download_id)
        if not download:
            return

        download_dir = _get_setting(session, "download_dir", str(Path.home() / "ROMs"))
        dest = Path(download_dir) / download.system / download.file_name

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
            client = ArchiveClient()
            await client.download_file(download.source_url, dest, on_progress)

            # Extract the ROM if it was downloaded as a zip
            rom_path = dest
            if dest.suffix.lower() == ".zip":
                rom_path = extract_rom_from_zip(dest)

            file_hash = hash_rom(rom_path, download.system)
            download.file_path = str(rom_path)
            download.file_name = rom_path.name
            download.file_hash = file_hash
            download.progress = 1.0
            download.status = DownloadStatus.completed

            # Verify hash against RetroAchievements if enabled
            ra_enabled = _get_setting(session, "ra_enabled", "false") == "true"
            ra_username = _get_setting(session, "ra_username")
            ra_api_key = _get_setting(session, "ra_api_key")
            if ra_enabled and ra_username and ra_api_key:
                from app.services.ra_client import RAClient
                ra = RAClient(ra_username, ra_api_key)
                try:
                    match = await ra.lookup_hash(file_hash)
                    if match:
                        download.hash_verified = True
                        download.status = DownloadStatus.verified
                except Exception:
                    pass  # RA lookup failure doesn't fail the download
        except Exception as exc:
            download.status = DownloadStatus.failed
            download.error_message = str(exc)

        download.updated_at = datetime.utcnow()
        session.add(download)
        session.commit()
