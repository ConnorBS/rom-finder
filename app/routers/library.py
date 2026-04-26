import json
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import AppSetting, LibraryEntry, WantedGame, HuntStatus
from app.services.ra_client import DEFAULT_FOLDER_MAP
from app.services import logger as applog

router = APIRouter(prefix="/library")
templates = Jinja2Templates(directory="app/templates")

ROM_EXTENSIONS = {
    ".nes", ".fds",                          # NES / Famicom Disk
    ".sfc", ".smc",                          # SNES
    ".gb", ".gbc",                           # Game Boy / Color
    ".gba",                                  # Game Boy Advance
    ".n64", ".z64", ".v64",                  # Nintendo 64
    ".nds",                                  # Nintendo DS
    ".3ds", ".cia",                          # 3DS
    ".md", ".gen",                           # Sega Genesis / Mega Drive
    ".sms",                                  # Sega Master System
    ".gg",                                   # Game Gear
    ".32x",                                  # Sega 32X
    ".pce",                                  # PC Engine / TurboGrafx-16
    ".ngp", ".ngc",                          # Neo Geo Pocket
    ".ws", ".wsc",                           # WonderSwan
    ".a26", ".a52", ".a78",                  # Atari
    ".lnx",                                  # Atari Lynx
    ".iso", ".cue", ".bin", ".img",          # Disc-based (PS1/PS2/Saturn/etc.)
    ".chd",                                  # Compressed Hunks of Data
    ".cso", ".pbp",                          # PSP
    ".rom",                                  # Generic ROM
    ".zip", ".7z",                           # Archives
}


ARCHIVE_EXTENSIONS = {".zip", ".7z"}


def _rom_title(f: "Path") -> str:
    """Strip archive suffix from title when a zip wraps a named ROM (e.g. game.nes.zip → game)."""
    stem = f.stem
    if f.suffix.lower() in ARCHIVE_EXTENSIONS and Path(stem).suffix.lower() in ROM_EXTENSIONS:
        return Path(stem).stem
    return stem


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _build_folder_to_system_map(folder_map: dict) -> dict[str, str]:
    """Reverse folder_map + DEFAULT_FOLDER_MAP into folder_name -> system."""
    reverse: dict[str, str] = {}
    for sys, folder in DEFAULT_FOLDER_MAP.items():
        reverse[folder] = sys
    for sys, folder in folder_map.items():
        if folder:
            reverse[folder] = sys
    return reverse


@router.get("", response_class=HTMLResponse)
async def library_page(
    request: Request,
    system: str = Query(default=""),
    q: str = Query(default=""),
    session: Session = Depends(get_session),
):
    statement = select(LibraryEntry)

    if system:
        statement = statement.where(LibraryEntry.system == system)
    if q:
        statement = statement.where(LibraryEntry.game_title.contains(q))

    entries = session.exec(statement.order_by(LibraryEntry.game_title)).all()
    systems = session.exec(
        select(LibraryEntry.system).distinct()
    ).all()

    applog.log_navigation("library", {
        "result_count": len(entries), "system_filter": system, "query": q,
    })

    return templates.TemplateResponse(
        request, "library.html",
        {"entries": entries, "systems": systems, "selected_system": system, "query": q},
    )


@router.post("/{library_id}/verify-ra", response_class=HTMLResponse)
async def verify_ra_library_entry(
    library_id: int,
    session: Session = Depends(get_session),
):
    """Look up a library entry's hash against RetroAchievements and update ra_matched."""
    from datetime import datetime
    from app.services.ra_client import RAClient

    entry = session.get(LibraryEntry, library_id)
    if not entry or not entry.file_hash:
        return HTMLResponse('<span class="text-gray-600 text-xs">No hash</span>')

    ra_username = _get_setting(session, "ra_username")
    ra_api_key = _get_setting(session, "ra_api_key")
    if not ra_username or not ra_api_key:
        return HTMLResponse(
            '<span class="text-yellow-500 text-xs" title="Add RA credentials in Settings">No RA creds</span>'
        )

    ra = RAClient(ra_username, ra_api_key)
    try:
        match = await ra.lookup_hash(entry.file_hash)
        if match:
            entry.ra_matched = True
            entry.hash_verified = True
            entry.ra_game_id = entry.ra_game_id or match.get("ID")
            session.add(entry)
            if entry.ra_game_id:
                wanted = session.exec(
                    select(WantedGame).where(WantedGame.ra_game_id == entry.ra_game_id)
                ).first()
                if wanted and wanted.status != HuntStatus.verified:
                    wanted.status = HuntStatus.verified
                    wanted.updated_at = datetime.utcnow()
                    session.add(wanted)
            session.commit()
            applog.log_action("library_verify_ra", {
                "library_id": library_id, "hash": entry.file_hash, "ra_game_id": entry.ra_game_id,
            })
            if entry.ra_game_id:
                return HTMLResponse(
                    f'<a href="https://retroachievements.org/game/{entry.ra_game_id}" target="_blank" rel="noopener"'
                    f' class="inline-flex items-center gap-1 text-green-400 text-xs hover:text-green-300 transition-colors"'
                    f' title="View on RetroAchievements">'
                    f'<svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
                    f'<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/></svg>'
                    f'RA Match</a>'
                )
            return HTMLResponse(
                '<span class="inline-flex items-center gap-1 text-green-400 text-xs">'
                '<svg class="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
                '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/></svg>'
                'RA Match</span>'
            )
        return HTMLResponse('<span class="text-gray-600 text-xs">Not in RA</span>')
    except Exception as exc:
        applog.warning("hash", f"Library RA verify failed: {exc}", {
            "library_id": library_id, "hash": entry.file_hash,
        })
        return HTMLResponse('<span class="text-red-500 text-xs">RA error</span>')


@router.post("/scan", response_class=HTMLResponse)
async def scan_rom_folder(session: Session = Depends(get_session)):
    """Scan the ROM directory and add discovered files to the library."""
    download_dir = _get_setting(session, "download_dir", "")
    if not download_dir:
        return HTMLResponse(
            '<span class="text-yellow-400 text-xs">No ROMs directory configured. Set it in Settings first.</span>'
        )

    folder_map = json.loads(_get_setting(session, "folder_map", "{}"))
    folder_to_system = _build_folder_to_system_map(folder_map)

    existing_paths = set(
        session.exec(select(LibraryEntry.file_path)).all()
    )

    base = Path(download_dir)
    if not base.exists():
        return HTMLResponse(
            f'<span class="text-yellow-400 text-xs">Directory not found: {download_dir}</span>'
        )

    added = 0
    for subdir in sorted(base.iterdir()):
        if not subdir.is_dir():
            continue
        system = folder_to_system.get(subdir.name, subdir.name)
        for f in sorted(subdir.rglob('*')):
            if not f.is_file() or f.suffix.lower() not in ROM_EXTENSIONS:
                continue
            file_path_str = str(f)
            if file_path_str in existing_paths:
                continue
            entry = LibraryEntry(
                game_title=_rom_title(f),
                system=system,
                file_name=f.name,
                file_path=file_path_str,
            )
            session.add(entry)
            existing_paths.add(file_path_str)
            added += 1

    session.commit()
    applog.log_action("library_scan", {"download_dir": download_dir, "added": added})

    if added:
        return HTMLResponse(
            f'<span class="text-green-400 text-xs">&#10003; Added {added} ROM{"s" if added != 1 else ""} to your library.</span>'
        )
    return HTMLResponse(
        '<span class="text-gray-400 text-xs">Scan complete — no new ROMs found.</span>'
    )
