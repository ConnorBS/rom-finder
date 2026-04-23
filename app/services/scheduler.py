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
    """Walk the ROMs directory and import untracked files into the library."""
    import json
    from app.routers.library import ROM_EXTENSIONS, _build_folder_to_system_map

    with Session(engine) as session:
        download_dir = _get(session, "download_dir", "")
        folder_map = json.loads(_get(session, "folder_map", "{}"))

    if not download_dir:
        return {"error": "No ROMs directory configured", "added": 0}
    base = Path(download_dir)
    if not base.exists():
        return {"error": f"Directory not found: {download_dir}", "added": 0}

    folder_to_system = _build_folder_to_system_map(folder_map)

    with Session(engine) as session:
        existing_paths = set(session.exec(select(LibraryEntry.file_path)).all())

    new_entries: list[LibraryEntry] = []
    for subdir in sorted(base.iterdir()):
        if not subdir.is_dir():
            continue
        system = folder_to_system.get(subdir.name, subdir.name)
        for f in sorted(subdir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in ROM_EXTENSIONS:
                continue
            fp = str(f)
            if fp in existing_paths:
                continue
            new_entries.append(LibraryEntry(game_title=f.stem, system=system, file_name=f.name, file_path=fp))
            existing_paths.add(fp)

    if new_entries:
        with Session(engine) as session:
            for e in new_entries:
                session.add(e)
            session.commit()

    added = len(new_entries)
    _set_last_run("sched_scan_last_run")
    applog.info("scheduler", f"Scan complete — {added} new ROMs")
    return {"added": added}


async def run_hash_check() -> dict:
    """Clear stale hashes (file modified since hashing), then hash all un-hashed entries."""
    from app.services.hasher import hash_rom
    from app.services.rahasher import compute_ra_hash
    from app.services import activity as activity_store

    with Session(engine) as session:
        all_entries = session.exec(select(LibraryEntry)).all()

    cleared = 0
    to_hash: list[int] = []

    with Session(engine) as session:
        for entry in session.exec(select(LibraryEntry)).all():
            p = Path(entry.file_path)
            if not p.exists():
                continue
            if entry.file_hash and entry.hashed_at:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if mtime > entry.hashed_at:
                    entry.file_hash = None
                    entry.ra_matched = False
                    entry.hash_verified = False
                    entry.hashed_at = None
                    session.add(entry)
                    cleared += 1
            if not entry.file_hash:
                to_hash.append(entry.id)
        session.commit()

    if not to_hash:
        _set_last_run("sched_hash_last_run")
        applog.info("scheduler", f"Hash check — {cleared} stale cleared, nothing new to hash")
        return {"cleared": cleared, "hashed": 0}

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
                continue
            try:
                result = await compute_ra_hash(p, entry.system)
                if result is None:
                    result = await loop.run_in_executor(None, hash_rom, p, entry.system)
                entry.file_hash = result
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
    applog.info("scheduler", f"Hash check — {cleared} stale cleared, {hashed} hashed")
    return {"cleared": cleared, "hashed": hashed}


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
