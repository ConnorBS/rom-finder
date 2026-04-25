import json
import re
from fastapi import APIRouter, Request, Form, Depends, Query, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from pathlib import Path

from app.db.database import get_session
from app.db.models import AppSetting
from app.services import sources as source_registry
from app.services import cover_sources as cover_source_registry
from app.services.ra_client import SYSTEMS, DEFAULT_FOLDER_MAP
from app.services import logger as applog

router = APIRouter(prefix="/settings")
templates = Jinja2Templates(directory="app/templates")

# All known system names from RA, sorted for dropdowns
KNOWN_SYSTEMS = sorted(SYSTEMS.values())

# Build a reverse lookup: folder name (lowercase) -> system name for auto-mapping
_FOLDER_LOWER_TO_SYSTEM: dict[str, str] = {}
for _sys, _folder in DEFAULT_FOLDER_MAP.items():
    _FOLDER_LOWER_TO_SYSTEM[_folder.lower()] = _sys
for _sys_name in SYSTEMS.values():
    _FOLDER_LOWER_TO_SYSTEM[_sys_name.lower()] = _sys_name


def get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def set_setting(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key) or AppSetting(key=key)
    s.value = value
    session.add(s)


def _scan_folders(path_str: str) -> list[str]:
    """Return sorted list of subdirectory names under path_str."""
    try:
        p = Path(path_str)
        if not p.exists() or not p.is_dir():
            return []
        return sorted(d.name for d in p.iterdir() if d.is_dir())
    except Exception:
        return []


def _automap_folder(folder_name: str) -> str:
    """Return best-match RA system name for a folder name, or '' if no match."""
    normalized = folder_name.lower().strip()

    # Exact match
    if normalized in _FOLDER_LOWER_TO_SYSTEM:
        return _FOLDER_LOWER_TO_SYSTEM[normalized]

    # Strip common noise words and retry
    stripped = re.sub(r"[_\-\.]", " ", normalized).strip()
    if stripped in _FOLDER_LOWER_TO_SYSTEM:
        return _FOLDER_LOWER_TO_SYSTEM[stripped]

    # Substring match — folder contains or is contained by a known name
    for key, sys_name in _FOLDER_LOWER_TO_SYSTEM.items():
        if key in normalized or normalized in key:
            return sys_name

    return ""


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, session: Session = Depends(get_session)):
    applog.log_navigation("settings")
    download_dir = get_setting(session, "download_dir", "/roms")
    raw_map = get_setting(session, "folder_map", "{}")
    folder_map = json.loads(raw_map) or dict(DEFAULT_FOLDER_MAP)
    current = {
        "download_dir": download_dir,
        "check_dir": get_setting(session, "check_dir", "/rom-check"),
        "covers_dir": get_setting(session, "covers_dir", "static/covers"),
        "ra_enabled": get_setting(session, "ra_enabled", "false"),
        "ra_username": get_setting(session, "ra_username"),
        "ra_api_key": get_setting(session, "ra_api_key"),
        "verbose_logging": get_setting(session, "verbose_logging", "false"),
        "use_review_dir": get_setting(session, "use_review_dir", "true"),
        "download_dir_readonly": get_setting(session, "download_dir_readonly", "false"),
        "check_dir_readonly": get_setting(session, "check_dir_readonly", "false"),
        "covers_dir_readonly": get_setting(session, "covers_dir_readonly", "false"),
        "ra_autodiscover_enabled": get_setting(session, "ra_autodiscover_enabled", "false"),
        "ra_autodiscover_last_checked": get_setting(session, "ra_autodiscover_last_checked", ""),
    }
    all_srcs = source_registry.all_sources()
    src_enabled = {
        src.source_id: get_setting(session, f"source_{src.source_id}_enabled", "false") == "true"
        for src in all_srcs
    }

    # Cover sources — reorder by saved priority
    all_cover_srcs = cover_source_registry.all_sources()
    order_raw = get_setting(session, "cover_sources_order", "")
    if order_raw:
        try:
            order = json.loads(order_raw)
            src_map = {s.source_id: s for s in all_cover_srcs}
            ordered = [src_map[sid] for sid in order if sid in src_map]
            ordered_ids = {s.source_id for s in ordered}
            ordered += [s for s in all_cover_srcs if s.source_id not in ordered_ids]
            all_cover_srcs = ordered
        except (ValueError, KeyError):
            pass
    cover_src_enabled = {
        src.source_id: get_setting(session, f"cover_source_{src.source_id}_enabled", "false") == "true"
        for src in all_cover_srcs
    }
    cover_src_api_keys = {
        src.source_id: get_setting(session, f"cover_source_{src.source_id}_api_key", "")
        for src in all_cover_srcs
        if src.requires_api_key
    }

    roms_folders = _scan_folders(download_dir)
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "settings": current,
            "sources": all_srcs,
            "source_enabled": src_enabled,
            "cover_sources": all_cover_srcs,
            "cover_source_enabled": cover_src_enabled,
            "cover_source_api_keys": cover_src_api_keys,
            "roms_folders": roms_folders,
            "folder_map": folder_map,
            "known_systems": KNOWN_SYSTEMS,
        },
    )


@router.post("", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    session: Session = Depends(get_session),
    download_dir: str = Form(...),
    check_dir: str = Form(...),
    covers_dir: str = Form(default="static/covers"),
    ra_username: str = Form(default=""),
    ra_api_key: str = Form(default=""),
):
    set_setting(session, "download_dir", download_dir)
    set_setting(session, "check_dir", check_dir)
    set_setting(session, "covers_dir", covers_dir)
    from pathlib import Path as _Path
    _Path(covers_dir).mkdir(parents=True, exist_ok=True)
    set_setting(session, "ra_username", ra_username)
    set_setting(session, "ra_api_key", ra_api_key)

    form_data = await request.form()

    ra_enabled = "true" if form_data.get("ra_enabled") == "true" else "false"
    set_setting(session, "ra_enabled", ra_enabled)

    verbose_logging = "true" if form_data.get("verbose_logging") == "true" else "false"
    set_setting(session, "verbose_logging", verbose_logging)

    # Review directory toggle + per-directory read-only toggles + autodiscover enable flag
    for key in ("use_review_dir", "download_dir_readonly", "check_dir_readonly", "covers_dir_readonly", "ra_autodiscover_enabled"):
        set_setting(session, key, "true" if form_data.get(key) == "true" else "false")

    # ROM source toggles
    for src in source_registry.all_sources():
        key = f"source_{src.source_id}_enabled"
        value = "true" if form_data.get(key) == "true" else "false"
        set_setting(session, key, value)

    # Cover art source order + toggles + API keys
    cover_order = form_data.getlist("cover_source_order[]")
    if cover_order:
        set_setting(session, "cover_sources_order", json.dumps(cover_order))
    for src in cover_source_registry.all_sources():
        enabled_key = f"cover_source_{src.source_id}_enabled"
        set_setting(session, enabled_key, "true" if form_data.get(enabled_key) == "true" else "false")
        if src.requires_api_key:
            api_key_val = form_data.get(f"cover_source_{src.source_id}_api_key", "")
            set_setting(session, f"cover_source_{src.source_id}_api_key", str(api_key_val))

    # Folder mapping — parallel arrays folder_names[] + folder_systems[]
    folder_names = form_data.getlist("folder_names[]")
    folder_systems = form_data.getlist("folder_systems[]")
    folder_map: dict[str, str] = {}
    for fname, fsys in zip(folder_names, folder_systems):
        if fsys:
            folder_map[fsys] = fname
    set_setting(session, "folder_map", json.dumps(folder_map))

    session.commit()

    enabled_srcs = [
        src.source_id for src in source_registry.all_sources()
        if form_data.get(f"source_{src.source_id}_enabled") == "true"
    ]
    applog.log_settings("Settings saved", {
        "download_dir": download_dir,
        "check_dir": check_dir,
        "covers_dir": covers_dir,
        "ra_enabled": ra_enabled,
        "ra_username": ra_username,
        "enabled_sources": enabled_srcs,
        "folder_map": folder_map,
    })

    return HTMLResponse(
        '<div id="settings-toast" class="bg-green-900/50 border border-green-700 '
        'text-green-300 px-4 py-3 rounded-lg text-sm">'
        'Settings saved.</div>'
    )


@router.post("/ra-test", response_class=HTMLResponse)
async def test_ra_credentials(
    ra_username: str = Form(default=""),
    ra_api_key: str = Form(default=""),
):
    if not ra_username or not ra_api_key:
        return HTMLResponse('<span class="text-yellow-400 text-xs">Enter username and API key first.</span>')
    from app.services.ra_client import RAClient
    ra = RAClient(ra_username, ra_api_key)
    ok, msg = await ra.test_credentials()
    applog.log_settings(f"RA credential test: {'passed' if ok else 'failed'}", {
        "username": ra_username, "result": msg,
    })
    if ok:
        return HTMLResponse(f'<span class="text-green-400 text-xs">&#10003; {msg}</span>')
    return HTMLResponse(f'<span class="text-red-400 text-xs">&#10007; {msg}</span>')


@router.get("/folder-scan", response_class=HTMLResponse)
async def folder_scan(
    path: str = Query(default=""),
    session: Session = Depends(get_session),
):
    """Rescan the download_dir and return updated folder rows partial."""
    scan_path = path or get_setting(session, "download_dir", "")
    folder_map = json.loads(get_setting(session, "folder_map", "{}"))
    folders = _scan_folders(scan_path)
    rows = _build_folder_rows(folders, folder_map)
    return HTMLResponse(rows)


@router.get("/folder-automap", response_class=HTMLResponse)
async def folder_automap(
    path: str = Query(default=""),
    session: Session = Depends(get_session),
):
    """Auto-suggest system mappings for each folder based on name matching."""
    scan_path = path or get_setting(session, "download_dir", "")
    folder_map = json.loads(get_setting(session, "folder_map", "{}"))
    folders = _scan_folders(scan_path)

    # Apply auto-mapping: prefer existing user map, then auto-detect
    suggested: dict[str, str] = {}
    for folder in folders:
        existing = next((sys for sys, f in folder_map.items() if f == folder), "")
        suggested[folder] = existing or _automap_folder(folder)

    rows = _build_folder_rows(folders, {sys: f for f, sys in suggested.items() if sys}, suggested)
    return HTMLResponse(rows)


@router.post("/autodiscover/run-now", response_class=HTMLResponse)
async def autodiscover_run_now(session: Session = Depends(get_session)):
    """Immediately run one autodiscover pass and return a result summary."""
    username = get_setting(session, "ra_username")
    api_key = get_setting(session, "ra_api_key")
    if not username or not api_key:
        return HTMLResponse('<span class="text-yellow-400 text-xs">Configure RetroAchievements credentials first.</span>')
    from app.services.autodiscover import run_autodiscover
    result = await run_autodiscover()
    if "error" in result:
        return HTMLResponse(f'<span class="text-red-400 text-xs">&#10007; {result["error"]}</span>')
    added = result.get("added", 0)
    systems = result.get("systems_checked", 0)
    if added > 0:
        return HTMLResponse(
            f'<span class="text-green-400 text-xs">&#10003; Added {added} game{"s" if added != 1 else ""}'
            f' across {systems} system{"s" if systems != 1 else ""}.</span>'
        )
    return HTMLResponse(
        f'<span class="text-gray-400 text-xs">No new games found across {systems} system{"s" if systems != 1 else ""}.</span>'
    )


def _build_folder_rows(
    folders: list[str],
    folder_map: dict[str, str],
    suggested: dict[str, str] | None = None,
) -> str:
    """Build HTML table rows for the folder mapping table."""
    rows = ""
    for folder in folders:
        assigned = next((sys for sys, f in folder_map.items() if f == folder), "")
        if suggested is not None:
            assigned = suggested.get(folder, assigned)
        options = '<option value="">— Not mapped —</option>'
        for sys in KNOWN_SYSTEMS:
            sel = "selected" if sys == assigned else ""
            options += f'<option value="{sys}" {sel}>{sys}</option>'
        rows += (
            f'<tr class="border-t border-gray-800">'
            f'<td class="py-2 pr-4 text-sm text-gray-300 font-mono">{folder}</td>'
            f'<td class="py-2">'
            f'<input type="hidden" name="folder_names[]" value="{folder}">'
            f'<select name="folder_systems[]" class="bg-gray-800 border border-gray-700 text-gray-200 rounded px-2 py-1 text-xs focus:outline-none focus:border-blue-500">'
            f'{options}</select>'
            f'</td>'
            f'</tr>'
        )
    if not rows:
        rows = '<tr><td colspan="2" class="py-4 text-gray-600 text-sm text-center">No subfolders found at that path.</td></tr>'
    return rows
