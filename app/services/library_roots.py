"""Multiple ROM directories ("library roots") — registration, console mapping, and
the one shared on-disk walk.

The library is the UNION of all registered `LibraryRoot`s (the combined collection
view). Exactly one root is `is_primary` — the default download/file target. Each root
carries its OWN folder→system map (manual per-directory mapping), so the same console
can live under different folder names in different directories (`psx` here, `Sony
PlayStation` there) and still group as one system.

Direction note: a root's `folder_map` JSON is **folder→system** (the scanner's direct
lookup + the natural shape for "assign a console to each folder"), NOT the legacy global
`folder_map` setting's system→folder. `dest_folder_for_system` inverts it when filing a
download.

Leaf-ish: imports `app.db`, `app.services.settings`, and `DEFAULT_FOLDER_MAP` (a plain
dict) from `ra_client`. The on-disk walk lazily imports the scan helpers from
`routers.library` (same pattern scheduler.run_scan already uses) to avoid a module-load
services→routers import.
"""
import json
import os
from pathlib import Path
from typing import Iterator, Optional

from sqlmodel import Session, select

from app.db.models import LibraryEntry, LibraryRoot
from app.services import settings as app_settings
from app.services.ra_client import DEFAULT_FOLDER_MAP


# DEFAULT_FOLDER_MAP is {system: folder}; reverse it once for folder→system fallback.
_DEFAULT_FOLDER_TO_SYSTEM: dict[str, str] = {
    folder: system for system, folder in DEFAULT_FOLDER_MAP.items()
}


def _norm(path: str) -> str:
    """Case/separator-normalized absolute-ish path for prefix comparison."""
    return os.path.normcase(os.path.normpath(path))


# --- Queries ---------------------------------------------------------------

def get_roots(session: Session) -> list[LibraryRoot]:
    return list(session.exec(select(LibraryRoot).order_by(LibraryRoot.position, LibraryRoot.id)).all())


def primary_root(session: Session) -> Optional[LibraryRoot]:
    roots = get_roots(session)
    for r in roots:
        if r.is_primary:
            return r
    return roots[0] if roots else None


def root_for_path(roots: list[LibraryRoot], path: str) -> Optional[LibraryRoot]:
    """Longest-prefix match of an absolute file path to its owning root."""
    target = _norm(path)
    best: Optional[LibraryRoot] = None
    best_len = -1
    for r in roots:
        rp = _norm(r.path)
        if target == rp or target.startswith(rp + os.sep):
            if len(rp) > best_len:
                best, best_len = r, len(rp)
    return best


# --- Console mapping -------------------------------------------------------

def _root_folder_map(root: LibraryRoot) -> dict[str, str]:
    """The root's folder→system map (guarded against corrupt JSON)."""
    try:
        data = json.loads(root.folder_map or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def resolve_system(root: LibraryRoot, folder_name: str) -> str:
    """folder_name → canonical system, using the root's map first, then the built-in
    default aliases, then the folder name itself (matches the legacy single-root behaviour)."""
    fm = _root_folder_map(root)
    if folder_name in fm and fm[folder_name]:
        return fm[folder_name]
    if folder_name in _DEFAULT_FOLDER_TO_SYSTEM:
        return _DEFAULT_FOLDER_TO_SYSTEM[folder_name]
    return folder_name


def dest_folder_for_system(root: LibraryRoot, system: str) -> str:
    """Folder name (under `root`) to FILE a ROM of `system` into. Inverts the root's
    folder→system map (first folder that maps to this system, deterministic), then the
    global default, then the system name itself."""
    fm = _root_folder_map(root)
    matches = sorted(folder for folder, sys in fm.items() if sys == system)
    if matches:
        return matches[0]
    return DEFAULT_FOLDER_MAP.get(system, system)


def reconcile_primary_path(session: Session) -> None:
    """Keep the primary root's path == the `download_dir` setting (the field that edits
    it). The Settings form syncs them explicitly, but anything that sets `download_dir`
    directly (tests, env-seeded defaults, the legacy single-dir flow) should still drive
    the primary directory — so scan/download chokepoints call this first. Seeds the
    primary if none exists yet."""
    root = primary_root(session)
    if root is None:
        ensure_primary_and_backfill(session)
        return
    dd = app_settings.get(session, "download_dir", "")
    if dd:
        norm = os.path.normpath(dd)
        if root.path != norm:
            root.path = norm
            session.add(root)
            session.commit()


def download_target(session: Session, system: str) -> tuple[str, str, Optional[int]]:
    """Where to FILE a downloaded/moved ROM of `system`: (base_dir, folder_name, root_id).
    Uses the primary root; falls back to the legacy `download_dir` setting when no roots
    exist yet (a fresh install before seeding)."""
    reconcile_primary_path(session)
    root = primary_root(session)
    if root is not None:
        return root.path, dest_folder_for_system(root, system), root.id
    download_dir = app_settings.get(session, "download_dir", "/roms")
    legacy = app_settings.get_json(session, "folder_map", {})
    folder = (legacy.get(system) if isinstance(legacy, dict) else None) or DEFAULT_FOLDER_MAP.get(system, system)
    return download_dir, folder, None


# --- Shared on-disk walk ---------------------------------------------------

def iter_rom_files(
    roots: list[LibraryRoot], cue_cache: dict | None = None
) -> Iterator[tuple[LibraryRoot, str, str, str, str]]:
    """Yield (root, system, title, file_name, file_path) for every standalone ROM file
    across ALL roots — the single walk reused by every scan path. Skips disc tracks
    (.bin/.img of a cuesheet) and non-ROM extensions. `cue_cache` memoises the per-folder
    cuesheet check across the whole sweep."""
    from app.routers.library import ROM_EXTENSIONS, _rom_title, is_disc_track

    if cue_cache is None:
        cue_cache = {}
    for root in roots:
        base = Path(root.path)
        if not base.exists() or not base.is_dir():
            continue
        for subdir in sorted(base.iterdir()):
            if not subdir.is_dir():
                continue
            system = resolve_system(root, subdir.name)
            for f in sorted(subdir.rglob("*")):
                if not f.is_file() or f.suffix.lower() not in ROM_EXTENSIONS:
                    continue
                if is_disc_track(f, cue_cache):
                    continue
                yield root, system, _rom_title(f), f.name, str(f)


# --- Startup seeding + backfill --------------------------------------------

def ensure_primary_and_backfill(session: Session) -> None:
    """Idempotent, run every startup. Seeds the primary root from the legacy
    `download_dir` setting (migrating the reversed global folder_map into its folder→system
    map) when no roots exist, guarantees exactly one primary, then backfills any
    LibraryEntry.root_id that's still NULL by longest-prefix path match."""
    roots = get_roots(session)
    if not roots:
        download_dir = app_settings.get(session, "download_dir", "")
        if download_dir:
            # Migrate the legacy global {system: folder} map → per-root {folder: system}.
            legacy = app_settings.get_json(session, "folder_map", {})
            folder_to_system = {
                folder: system for system, folder in legacy.items() if folder
            } if isinstance(legacy, dict) else {}
            session.add(LibraryRoot(
                path=os.path.normpath(download_dir),
                label="Main",
                is_primary=True,
                position=0,
                folder_map=json.dumps(folder_to_system),
            ))
            session.commit()
            roots = get_roots(session)
    elif not any(r.is_primary for r in roots):
        roots[0].is_primary = True
        session.add(roots[0])
        session.commit()

    if not roots:
        return

    # Backfill root_id for legacy entries (or any left null).
    unassigned = session.exec(
        select(LibraryEntry).where(LibraryEntry.root_id == None)  # noqa: E711
    ).all()
    changed = False
    for entry in unassigned:
        r = root_for_path(roots, entry.file_path)
        if r is not None:
            entry.root_id = r.id
            session.add(entry)
            changed = True
    if changed:
        session.commit()
