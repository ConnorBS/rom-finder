"""Scheduled task runner — fires configured tasks at their daily local-time slot."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import AppSetting, LibraryEntry
from app.services import logger as applog


def _get(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _set_last_run(key: str) -> None:
    with Session(engine) as session:
        s = session.get(AppSetting, key) or AppSetting(key=key)
        s.value = datetime.utcnow().isoformat()
        session.add(s)
        session.commit()


def _should_run(last_run_str: str, time_str: str) -> bool:
    """True if the local scheduled time has passed today and the task hasn't run since."""
    now = datetime.now()
    try:
        h, m = (int(x) for x in time_str.split(":"))
    except (ValueError, AttributeError):
        h, m = 4, 0
    scheduled_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < scheduled_today:
        return False
    if not last_run_str:
        return True
    try:
        last = datetime.fromisoformat(last_run_str)
        return last < scheduled_today
    except ValueError:
        return True


async def run_scan() -> dict:
    """Scan for new ROMs, then hash + fetch covers + RA-verify each newly-found file."""
    import json
    from app.routers.library import ROM_EXTENSIONS, _build_folder_to_system_map, _rom_title
    from app.services.hasher import hash_rom
    from app.services.rahasher import compute_ra_hash
    from app.services import activity as activity_store

    with Session(engine) as session:
        download_dir = _get(session, "download_dir", "")
        folder_map = json.loads(_get(session, "folder_map", "{}"))
        ra_username = _get(session, "ra_username")
        ra_api_key = _get(session, "ra_api_key")
        covers_readonly = _get(session, "covers_dir_readonly", "false") == "true"

    if not download_dir:
        return {"error": "No ROMs directory configured"}
    base = Path(download_dir)
    if not base.exists():
        return {"error": f"ROMs directory not found: {download_dir}"}

    folder_to_system = _build_folder_to_system_map(folder_map)

    with Session(engine) as session:
        existing_paths = set(session.exec(select(LibraryEntry.file_path)).all())

    # --- Step 1: find new files ---
    new_files: list[tuple[str, str, str, str]] = []  # (title, system, fname, fpath)
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
            new_files.append((_rom_title(f), system, f.name, fp))
            existing_paths.add(fp)

    if not new_files:
        _set_last_run("sched_scan_last_run")
        applog.info("scheduler", "Scan — library up to date, no new ROMs found")
        return {"added": 0, "hashed": 0, "verified": 0}

    # --- Step 2: insert into DB, collecting new IDs ---
    new_ids: list[int] = []
    with Session(engine) as session:
        for title, system, fname, fpath in new_files:
            e = LibraryEntry(game_title=title, system=system, file_name=fname, file_path=fpath)
            session.add(e)
            session.flush()
            new_ids.append(e.id)
        session.commit()

    added = len(new_ids)
    applog.info("scheduler", f"Scan: {added} new ROMs — hashing + covers + RA verify")

    # --- Step 3: hash each new entry ---
    loop = asyncio.get_event_loop()
    batch_id = "scan-hash-batch"
    activity_store.start_batch(
        batch_id,
        f"Hashing {added} new ROM{'s' if added != 1 else ''}",
        added, "rehash", entry_ids=new_ids,
    )
    hashed = 0
    with Session(engine) as session:
        for eid in new_ids:
            entry = session.get(LibraryEntry, eid)
            if not entry:
                activity_store.increment(batch_id)
                continue
            p = Path(entry.file_path)
            if not p.exists():
                activity_store.increment(batch_id)
                continue
            try:
                h = await compute_ra_hash(p, entry.system)
                if h is None:
                    h = await loop.run_in_executor(None, hash_rom, p, entry.system)
                entry.file_hash = h
                entry.hashed_at = datetime.utcnow()
                session.add(entry)
                hashed += 1
            except Exception as exc:
                applog.warning("scheduler", f"Hash failed for {entry.file_name}: {exc}")
            activity_store.increment(batch_id)
        session.commit()

    # --- Step 4: fetch covers ---
    if not covers_readonly:
        from app.routers.collection import _fetch_cover_for_library
        for eid in new_ids:
            with Session(engine) as session:
                entry = session.get(LibraryEntry, eid)
                if not entry:
                    continue
            await _fetch_cover_for_library(entry.id, entry.ra_game_id, entry.game_title, entry.system)

    # --- Step 5: RA verify ---
    verified = 0
    if ra_username and ra_api_key:
        from app.services.ra_client import RAClient
        ra = RAClient(ra_username, ra_api_key)
        with Session(engine) as session:
            for eid in new_ids:
                entry = session.get(LibraryEntry, eid)
                if not entry or not entry.file_hash:
                    continue
                try:
                    match = await ra.lookup_hash(entry.file_hash)
                    if match:
                        entry.ra_matched = True
                        entry.hash_verified = True
                        entry.ra_game_id = entry.ra_game_id or match.get("ID")
                        session.add(entry)
                        verified += 1
                except Exception as exc:
                    applog.warning("scheduler", f"RA verify failed for {entry.file_name}: {exc}")
            session.commit()

    _set_last_run("sched_scan_last_run")
    applog.info("scheduler", f"Scan pipeline: {added} added, {hashed} hashed, {verified} RA matched")
    return {"added": added, "hashed": hashed, "verified": verified}


async def run_hash_check() -> dict:
    """
    Three passes over the library:
      1. Backfill hashed_at=now for entries that have a hash but no timestamp
         (entries hashed before the hashed_at column existed).
      2. Clear hashes where the file's mtime is newer than hashed_at (file changed).
      3. Hash every entry that still has no hash.
    """
    from app.services.hasher import hash_rom
    from app.services.rahasher import compute_ra_hash
    from app.services import activity as activity_store

    backfilled = 0
    cleared = 0
    skipped = 0
    to_hash: list[int] = []

    with Session(engine) as session:
        for entry in session.exec(select(LibraryEntry)).all():
            p = Path(entry.file_path)
            if not p.exists():
                skipped += 1
                continue

            if entry.file_hash:
                if entry.hashed_at is None:
                    # Backfill timestamp so future stale checks can work
                    entry.hashed_at = datetime.utcnow()
                    session.add(entry)
                    backfilled += 1
                else:
                    mtime = datetime.fromtimestamp(p.stat().st_mtime)
                    if mtime > entry.hashed_at:
                        entry.file_hash = None
                        entry.ra_matched = False
                        entry.hash_verified = False
                        entry.hashed_at = None
                        session.add(entry)
                        cleared += 1
            else:
                to_hash.append(entry.id)

        session.commit()

    if skipped:
        applog.info("scheduler", f"Hash check: {skipped} entries skipped (files not accessible)")

    if not to_hash:
        _set_last_run("sched_hash_last_run")
        applog.info("scheduler", f"Hash check — {backfilled} timestamps backfilled, {cleared} stale cleared, no un-hashed entries")
        return {"backfilled": backfilled, "cleared": cleared, "hashed": 0, "skipped": skipped}

    batch_id = "sched-hash-batch"
    activity_store.start_batch(
        batch_id,
        f"Hashing {len(to_hash)} ROM{'s' if len(to_hash) != 1 else ''}",
        len(to_hash), "rehash", entry_ids=to_hash,
    )

    loop = asyncio.get_event_loop()
    hashed = 0
    with Session(engine) as session:
        for eid in to_hash:
            entry = session.get(LibraryEntry, eid)
            if not entry:
                activity_store.increment(batch_id)
                continue
            p = Path(entry.file_path)
            if not p.exists():
                activity_store.increment(batch_id)
                skipped += 1
                continue
            try:
                h = await compute_ra_hash(p, entry.system)
                if h is None:
                    h = await loop.run_in_executor(None, hash_rom, p, entry.system)
                entry.file_hash = h
                entry.hashed_at = datetime.utcnow()
                entry.hash_verified = False
                entry.ra_matched = False
                session.add(entry)
                hashed += 1
            except Exception as exc:
                applog.warning("scheduler", f"Hash failed for {entry.file_name}: {exc}")
            activity_store.increment(batch_id)
        session.commit()

    _set_last_run("sched_hash_last_run")
    applog.info("scheduler", f"Hash check — {backfilled} backfilled, {cleared} cleared, {hashed} hashed, {skipped} skipped")
    return {"backfilled": backfilled, "cleared": cleared, "hashed": hashed, "skipped": skipped}


async def run_autodiscover() -> dict:
    from app.services.autodiscover import run_autodiscover as _run
    result = await _run()
    _set_last_run("sched_autodiscover_last_run")
    return result


async def scheduler_loop() -> None:
    """Wake every minute; fire tasks whose scheduled local time has arrived."""
    while True:
        await asyncio.sleep(60)
        with Session(engine) as session:
            task_configs = {
                "scan": (
                    _get(session, "sched_scan_enabled", "true"),
                    _get(session, "sched_scan_time", "04:00"),
                    _get(session, "sched_scan_last_run", ""),
                ),
                "hash": (
                    _get(session, "sched_hash_enabled", "true"),
                    _get(session, "sched_hash_time", "04:00"),
                    _get(session, "sched_hash_last_run", ""),
                ),
                "autodiscover": (
                    _get(session, "sched_autodiscover_enabled", "true"),
                    _get(session, "sched_autodiscover_time", "04:00"),
                    _get(session, "sched_autodiscover_last_run", ""),
                ),
            }

        runners = {"scan": run_scan, "hash": run_hash_check, "autodiscover": run_autodiscover}
        for task_name, (enabled, time_str, last_run) in task_configs.items():
            if enabled != "true":
                continue
            if not _should_run(last_run, time_str):
                continue
            try:
                await runners[task_name]()
            except Exception as exc:
                applog.warning("scheduler", f"Task '{task_name}' failed: {exc}")
