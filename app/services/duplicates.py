"""Tag redundant library copies via `LibraryEntry.duplicate_of`.

Two entries are duplicates when they hold the SAME content (identical hash) OR are the
SAME game in a different container/copy (same normalized title + system). They are NOT
duplicates when they are different discs of a multi-disc game (the disc marker lives in
the title, so "(Disc 1)" and "(Disc 2)" never merge), and a `.bin`/`.img` track (a
*component* of a `.cue`/`.gdi`) is never tagged — those are "subsets", and deleting one
would break the disc it belongs to.

**`ra_game_id` is deliberately NOT a grouping key.** RA files many genuinely DIFFERENT
ROMs under one game id — hack collections and "subset" games (e.g. dozens of distinct
SM64 romhacks all map to one id) — so grouping by it tagged unrelated games as
duplicates of each other (and, with the delete buttons, invited deleting the wrong one).
Identical-hash + same-title is the safe signal; once the archive-unwrap fix re-hashes a
`.7z`/`.chd` copy to match its `.cue` sibling, identical-hash catches it anyway.

`recompute_duplicates` is a full rebuild: it clears every flag then re-derives them,
so resolved/removed duplicates clear cleanly (never append-only). Cheap enough to run
after any hashing/verify/scan pass that can change hashes.
"""

from pathlib import PurePath

from sqlmodel import Session, select

from app.db.models import LibraryEntry

# Disc TRACK extensions — a component of a .cue/.gdi, not a standalone game. Excluded
# so we never tag (and tempt deletion of) a file the disc descriptor depends on.
_TRACK_EXTS = {".bin", ".img"}
# Archive containers rank below real disc images when choosing which copy to keep.
_ARCHIVE_EXTS = {".zip", ".7z"}


def _norm_title(t: str) -> str:
    # Keep region/disc/rev tags (they distinguish genuinely different dumps/discs);
    # just normalize whitespace + case.
    return " ".join(t.lower().split())


class _UnionFind:
    def __init__(self):
        self._p: dict[int, int] = {}

    def find(self, x: int) -> int:
        self._p.setdefault(x, x)
        root = x
        while self._p[root] != root:
            root = self._p[root]
        while self._p[x] != root:        # path compression
            self._p[x], x = root, self._p[x]
        return root

    def union(self, a: int, b: int) -> None:
        self._p[self.find(b)] = self.find(a)


def _canonical_rank(e: LibraryEntry) -> tuple:
    """Lower sorts first = the copy we KEEP as canonical."""
    suffix = PurePath(e.file_name).suffix.lower()
    return (
        0 if e.ra_matched else 1,             # keep the RA-verified copy
        1 if suffix in _ARCHIVE_EXTS else 0,  # keep a real image over a .zip/.7z
        e.id or 0,                            # stable: oldest entry id
    )


def recompute_duplicates(session: Session) -> dict:
    entries = session.exec(select(LibraryEntry)).all()
    for e in entries:                         # full rebuild — clear, then re-derive
        e.duplicate_of = None

    # Only present, non-track entries are candidates (tracks are subsets, see above).
    pool = [e for e in entries
            if not e.missing
            and PurePath(e.file_name).suffix.lower() not in _TRACK_EXTS
            and e.id is not None]

    uf = _UnionFind()
    by_key: dict[tuple, int] = {}
    for e in pool:
        uf.find(e.id)
        keys: list[tuple] = [("title", _norm_title(e.game_title), e.system)]
        if e.file_hash:
            keys.append(("hash", e.file_hash))
        for k in keys:
            other = by_key.get(k)
            if other is not None:
                uf.union(other, e.id)
            else:
                by_key[k] = e.id

    comps: dict[int, list[LibraryEntry]] = {}
    for e in pool:
        comps.setdefault(uf.find(e.id), []).append(e)

    groups = dups = 0
    for members in comps.values():
        if len(members) < 2:
            continue
        groups += 1
        canonical = min(members, key=_canonical_rank)
        for e in members:
            if e.id != canonical.id:
                e.duplicate_of = canonical.id
                dups += 1

    session.commit()
    return {"groups": groups, "duplicates": dups}
