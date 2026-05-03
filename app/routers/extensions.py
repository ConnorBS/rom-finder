"""Extension manager — browse, install, remove, and update ROM Finder extensions."""

import json
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import AppSetting, InstalledExtension
from app.services import extension_loader

router = APIRouter(prefix="/extensions")
templates = Jinja2Templates(directory="app/templates")


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _set_setting(session: Session, key: str, value: str) -> None:
    existing = session.get(AppSetting, key)
    if existing:
        existing.value = value
        session.add(existing)
    else:
        session.add(AppSetting(key=key, value=value))
    session.commit()


def _build_ext_config(session: Session, ext_id: str) -> dict:
    """Read ext_{ext_id}_* settings from DB into a config dict."""
    prefix = f"ext_{ext_id}_"
    all_settings = session.exec(select(AppSetting)).all()
    return {s.key[len(prefix):]: s.value for s in all_settings if s.key.startswith(prefix)}


def _version_gt(v1: str, v2: str) -> bool:
    """True if v1 > v2 using simple numeric tuple comparison."""
    try:
        return tuple(int(x) for x in v1.split(".")) > tuple(int(x) for x in v2.split("."))
    except (ValueError, AttributeError):
        return False


@router.get("", response_class=HTMLResponse)
async def extensions_page(request: Request, session: Session = Depends(get_session)):
    installed = session.exec(select(InstalledExtension).order_by(InstalledExtension.installed_at)).all()
    repos_raw = _get_setting(session, "extension_repos", "[]")
    try:
        repos = json.loads(repos_raw)
    except (ValueError, TypeError):
        repos = []
    return templates.TemplateResponse(
        request, "extensions.html", {"installed": installed, "repos": repos}
    )


@router.get("/fetch-available", response_class=HTMLResponse)
async def fetch_available(request: Request, session: Session = Depends(get_session)):
    """HTMX: fetch extension listings from all configured repos."""
    repos_raw = _get_setting(session, "extension_repos", "[]")
    try:
        repos = json.loads(repos_raw)
    except (ValueError, TypeError):
        repos = []

    installed_map = {
        e.ext_id: e for e in session.exec(select(InstalledExtension)).all()
    }

    available: list[dict] = []
    errors: list[str] = []

    for repo_url in repos:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(repo_url)
                resp.raise_for_status()
                data = resp.json()
            for ext in data.get("extensions", []):
                ext_id = ext.get("id", "")
                installed_ext = installed_map.get(ext_id)
                ext["_installed"] = installed_ext is not None
                if installed_ext:
                    ext["_installed_version"] = installed_ext.version
                    ext["_update_available"] = _version_gt(
                        ext.get("version", ""), installed_ext.version
                    )
                else:
                    ext["_installed_version"] = ""
                    ext["_update_available"] = False
                available.append(ext)
        except Exception as e:
            errors.append(f"{repo_url}: {e}")

    return templates.TemplateResponse(
        request, "partials/extension_available.html",
        {"available": available, "errors": errors}
    )


@router.post("/repos/add", response_class=HTMLResponse)
async def add_repo(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    url = str(form.get("url", "")).strip()

    repos_raw = _get_setting(session, "extension_repos", "[]")
    try:
        repos = json.loads(repos_raw)
    except (ValueError, TypeError):
        repos = []

    if url and url not in repos:
        repos.append(url)
        _set_setting(session, "extension_repos", json.dumps(repos))

    return templates.TemplateResponse(
        request, "partials/extension_repos.html", {"repos": repos}
    )


@router.post("/repos/remove", response_class=HTMLResponse)
async def remove_repo(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    url = str(form.get("url", "")).strip()

    repos_raw = _get_setting(session, "extension_repos", "[]")
    try:
        repos = json.loads(repos_raw)
    except (ValueError, TypeError):
        repos = []

    repos = [r for r in repos if r != url]
    _set_setting(session, "extension_repos", json.dumps(repos))

    return templates.TemplateResponse(
        request, "partials/extension_repos.html", {"repos": repos}
    )


@router.post("/install", response_class=HTMLResponse)
async def install_extension(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    ext_id = str(form.get("ext_id", "")).strip()
    name = str(form.get("name", "")).strip()
    version = str(form.get("version", "1.0.0")).strip()
    ext_type = str(form.get("ext_type", "rom_source")).strip()
    author = str(form.get("author", "")).strip()
    description = str(form.get("description", "")).strip()
    pkg_url = str(form.get("pkg_url", "")).strip()

    if not ext_id or not pkg_url:
        return HTMLResponse(
            '<p class="text-red-400 text-sm p-4">Missing extension ID or package URL.</p>'
        )

    ext_dir = _get_setting(session, "extensions_dir", "extensions")
    ext_path = Path(ext_dir) / f"{ext_id}.py"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(pkg_url)
            resp.raise_for_status()
            code = resp.text
    except Exception as e:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm p-4">Download failed: {e}</p>'
        )

    try:
        Path(ext_dir).mkdir(parents=True, exist_ok=True)
        ext_path.write_text(code, encoding="utf-8")
    except Exception as e:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm p-4">Failed to save extension: {e}</p>'
        )

    loaded_info = extension_loader.load_extension_file(ext_path)
    if loaded_info is None:
        ext_path.unlink(missing_ok=True)
        return HTMLResponse(
            f'<p class="text-red-400 text-sm p-4">'
            f'Extension loaded but failed validation. Check that {ext_id}.py exports '
            f'EXTENSION_INFO and SOURCE_CLASS / COVER_SOURCE_CLASS.</p>'
        )

    # Seed default values for any extension settings
    schemas = extension_loader.get_settings_schemas().get(ext_id, [])
    for setting in schemas:
        key = f"ext_{ext_id}_{setting['key']}"
        if not session.get(AppSetting, key):
            default = setting.get("default", "")
            if isinstance(default, bool):
                default = "true" if default else "false"
            session.add(AppSetting(key=key, value=str(default)))

    existing = session.exec(
        select(InstalledExtension).where(InstalledExtension.ext_id == ext_id)
    ).first()
    if existing:
        existing.name = name
        existing.version = version
        existing.ext_type = ext_type
        existing.author = author
        existing.description = description
        existing.pkg_url = pkg_url
        existing.file_name = f"{ext_id}.py"
        existing.enabled = True
        existing.updated_at = datetime.utcnow()
        session.add(existing)
    else:
        session.add(InstalledExtension(
            ext_id=ext_id,
            name=name,
            version=version,
            ext_type=ext_type,
            author=author,
            description=description,
            pkg_url=pkg_url,
            file_name=f"{ext_id}.py",
            enabled=True,
        ))

    enabled_key = f"source_{ext_id}_enabled" if ext_type == "rom_source" else f"cover_source_{ext_id}_enabled"
    _set_setting(session, enabled_key, "true")
    session.commit()

    return HTMLResponse("", headers={"HX-Redirect": "/extensions"})


@router.post("/{ext_id}/toggle", response_class=HTMLResponse)
async def toggle_extension(ext_id: str, request: Request, session: Session = Depends(get_session)):
    ext = session.exec(
        select(InstalledExtension).where(InstalledExtension.ext_id == ext_id)
    ).first()
    if not ext:
        return HTMLResponse("", headers={"HX-Redirect": "/extensions"})

    ext.enabled = not ext.enabled
    ext.updated_at = datetime.utcnow()
    session.add(ext)

    enabled_key = f"source_{ext_id}_enabled" if ext.ext_type == "rom_source" else f"cover_source_{ext_id}_enabled"

    if ext.enabled:
        ext_dir = _get_setting(session, "extensions_dir", "extensions")
        ext_path = Path(ext_dir) / ext.file_name
        config = _build_ext_config(session, ext_id)
        extension_loader.load_extension_file(ext_path, config or None)
        _set_setting(session, enabled_key, "true")
    else:
        extension_loader.unload_extension(ext_id)
        _set_setting(session, enabled_key, "false")

    session.commit()
    return HTMLResponse("", headers={"HX-Redirect": "/extensions"})


@router.post("/{ext_id}/update", response_class=HTMLResponse)
async def update_extension(ext_id: str, request: Request, session: Session = Depends(get_session)):
    ext = session.exec(
        select(InstalledExtension).where(InstalledExtension.ext_id == ext_id)
    ).first()
    if not ext:
        return HTMLResponse("", headers={"HX-Redirect": "/extensions"})

    ext_dir = _get_setting(session, "extensions_dir", "extensions")
    ext_path = Path(ext_dir) / ext.file_name

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(ext.pkg_url)
            resp.raise_for_status()
            code = resp.text
    except Exception as e:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm p-4">Update download failed: {e}</p>'
        )

    try:
        ext_path.write_text(code, encoding="utf-8")
    except Exception as e:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm p-4">Failed to write update: {e}</p>'
        )

    extension_loader.unload_extension(ext_id)
    config = _build_ext_config(session, ext_id)
    loaded_info = extension_loader.load_extension_file(ext_path, config or None)
    if loaded_info is None:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm p-4">Updated file failed to load. Check {ext_id}.py.</p>'
        )

    ext.version = loaded_info.get("version", ext.version)
    ext.updated_at = datetime.utcnow()
    session.add(ext)
    session.commit()

    return HTMLResponse("", headers={"HX-Redirect": "/extensions"})


@router.post("/{ext_id}/uninstall", response_class=HTMLResponse)
async def uninstall_extension(ext_id: str, request: Request, session: Session = Depends(get_session)):
    ext = session.exec(
        select(InstalledExtension).where(InstalledExtension.ext_id == ext_id)
    ).first()
    if not ext:
        return HTMLResponse("", headers={"HX-Redirect": "/extensions"})

    ext_dir = _get_setting(session, "extensions_dir", "extensions")
    ext_path = Path(ext_dir) / ext.file_name
    ext_path.unlink(missing_ok=True)

    extension_loader.unload_extension(ext_id)

    enabled_key = f"source_{ext_id}_enabled" if ext.ext_type == "rom_source" else f"cover_source_{ext_id}_enabled"
    _set_setting(session, enabled_key, "false")

    session.delete(ext)
    session.commit()

    return HTMLResponse("", headers={"HX-Redirect": "/extensions"})
