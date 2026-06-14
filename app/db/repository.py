"""Shared data-access helpers.

A deliberately small seam — NOT a general ORM wrapper. Only add a function here
when the same query/mutation is hand-written in 3+ places and drifting risks
correctness (e.g. the "mark the matching wanted game verified" block that was
copy-pasted into all three download-approval paths). Simple one-off queries stay
inline at their call site.

Leaf module: imports only sqlmodel + app.db.models. Callers pass an explicit
`session` so they keep control of session lifetime (never held across an await).
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app.db.models import Download, LibraryEntry, WantedGame, HuntStatus


def wanted_by_ra_game_id(session: Session, ra_game_id: int) -> Optional[WantedGame]:
    return session.exec(
        select(WantedGame).where(WantedGame.ra_game_id == ra_game_id)
    ).first()


def unverified_library_entries(session: Session) -> list[LibraryEntry]:
    """Entries that are hashed but not RA-matched — the `no_ra` set and the work
    set for the resumable verify (Phase 5)."""
    return list(session.exec(
        select(LibraryEntry).where(
            LibraryEntry.file_hash != None,   # noqa: E711
            LibraryEntry.ra_matched == False,  # noqa: E712
        )
    ).all())


def library_pending_ra_check(session: Session, stale_days: int = 7, limit: Optional[int] = None,
                             exclude_systems: Optional[set[str]] = None) -> list[LibraryEntry]:
    """Hashed-but-unmatched entries due for an RA re-check: never checked, or last
    checked more than `stale_days` ago. The Phase 5 resumable-verify work set —
    bounded so passes terminate and genuine misses aren't re-hammered daily.
    `exclude_systems` drops platforms RA can't verify (passed by the caller so this
    module stays db-pure — see ra_client.RA_UNSUPPORTED_SYSTEMS)."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=stale_days)
    stmt = select(LibraryEntry).where(
        LibraryEntry.file_hash != None,    # noqa: E711
        LibraryEntry.ra_matched == False,  # noqa: E712
        (LibraryEntry.ra_checked_at == None) | (LibraryEntry.ra_checked_at < cutoff),  # noqa: E711
    ).order_by(LibraryEntry.ra_checked_at)  # nulls (never-checked) first in SQLite
    if exclude_systems:
        stmt = stmt.where(LibraryEntry.system.not_in(exclude_systems))
    if limit:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def mark_wanted_verified(session: Session, ra_game_id: Optional[int]) -> Optional[WantedGame]:
    """Flip the matching WantedGame to verified. No-op (returns None) when there's
    no ra_game_id or no matching wanted entry. Caller commits."""
    if not ra_game_id:
        return None
    wanted = wanted_by_ra_game_id(session, ra_game_id)
    if wanted and wanted.status != HuntStatus.verified:
        wanted.status = HuntStatus.verified
        wanted.updated_at = datetime.utcnow()
        session.add(wanted)
    return wanted


def create_library_entry_from_download(
    session: Session,
    download: Download,
    file_path: Path | str,
    file_hash: Optional[str] = None,
    root_id: Optional[int] = None,
) -> LibraryEntry:
    """Build + add (not commit) a LibraryEntry from a completed Download.
    `file_path` is the final on-disk path; `file_hash` overrides download.file_hash
    when the hash was computed in this pass (the _run_download path); `root_id` is the
    owning LibraryRoot (the primary download target). Caller commits."""
    p = Path(file_path)
    try:
        file_size = p.stat().st_size   # so size sort + detail size work without a rescan
    except OSError:
        file_size = 0
    entry = LibraryEntry(
        game_title=download.game_title,
        system=download.system,
        file_name=p.name,
        file_path=str(p),
        file_hash=file_hash if file_hash is not None else download.file_hash,
        hash_verified=download.hash_verified,
        ra_game_id=download.ra_game_id,
        ra_matched=download.hash_verified,
        file_size=file_size,
        root_id=root_id,
    )
    session.add(entry)
    return entry
