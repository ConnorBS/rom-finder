"""Award tiers for owned ROMs, derived from the local RA dashboard mirror.

LOCAL only (zero RA calls): reads `ra_game_progress` (the manually-refreshed mirror)
and stamps each `LibraryEntry.ra_award` with that game's highest RA award tier, so the
collection can badge Mastered / Completed / Beaten without any RA round-trip.

"Mastered" = hardcore 100% (`highest_award_kind == "mastered"`). Subsets do NOT gate
it — a base game can be mastered while its subsets are still open; subsets are surfaced
separately (read-only, informational) by `services/subsets.py`.
"""
import re

from sqlmodel import Session, select

from app.db.models import LibraryEntry, RAGameProgress
from app.services import logger as applog
from app.services.duplicates import _is_subset, _norm_title

# Strip a trailing RA "[Subset - X]" / "(Subset - X)" segment to get the parent title.
_SUBSET_SPLIT_RE = re.compile(r"\s*[(\[]\s*subset\b.*$", re.IGNORECASE)

_BEATEN_KINDS = ("beaten", "beaten-softcore", "completed")


def base_title(title: str) -> str:
    return _norm_title(_SUBSET_SPLIT_RE.sub("", title or ""))


def sync_library_awards(session: Session) -> dict:
    """Stamp every LibraryEntry.ra_award from the RA mirror. Full rebuild (clears to
    "" when no mirror row), so retroactively-changed awards reconcile on each run."""
    rows = session.exec(select(RAGameProgress)).all()
    by_game_id = {r.game_id: r for r in rows}
    by_norm_title = {_norm_title(r.title): r for r in rows if r.title}

    entries = session.exec(select(LibraryEntry)).all()
    mastered = beaten = 0
    for e in entries:
        row = None
        if _is_subset(e):
            # A subset copy usually shares the base game's hash, so its ra_game_id can
            # point at the BASE game — resolve the subset's own progress row by title
            # first so its award reflects the subset, not the base.
            row = by_norm_title.get(_norm_title(e.game_title))
        if row is None and e.ra_game_id:
            row = by_game_id.get(e.ra_game_id)
        award = row.highest_award_kind if row else ""
        if e.ra_award != award:
            e.ra_award = award
            session.add(e)
        if award == "mastered":
            mastered += 1
        elif award in _BEATEN_KINDS:
            beaten += 1
    session.commit()
    applog.info("system", "Library awards synced", {"mastered": mastered, "beaten": beaten})
    return {"mastered": mastered, "beaten": beaten}
