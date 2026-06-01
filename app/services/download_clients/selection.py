"""Decide whether a torrent/usenet release is usable for a wanted game, and which
files to keep. Pure/local (no network) — unit-tested.

User rules:
  - single-game release → OK.
  - multi-file TORRENT that's a PACK of unrelated games → OK only if we can pick
    the wanted title's file(s) (qBittorrent file-priority); keep just those.
  - multi-disc game (one game, several disc files) → NOT a pack; keep ALL discs.
  - usenet release can't be trimmed per file, so a usenet PACK is skipped.

Matching reuses `hunter._file_score` (RA-accepted-name / title-term scoring) so a
torrent's files are judged exactly like a hunt's download candidates.
"""
import re
from pathlib import Path

from app.services.hunter import _file_score
from app.services.title_utils import search_title, significant_terms, title_is_relevant

# A disc marker inside a filename: "(Disc 1)", "Disk 2", "CD3", "(Disc 1 of 2)".
_DISC_RE = re.compile(r'[\s(_\-]*(?:disc|disk|cd|dvd|side)\s*\d+', re.IGNORECASE)

# Title/name signals that a release is a multi-game pack / full set, not one game.
_PACK_RE = re.compile(
    r'\b(?:collection|romset|rom\s*set|full\s*set|no[-\s]?intro|redump|merged|'
    r'\d{2,}\s*(?:in\s*1|games|roms|titles)|complete\s*(?:set|collection)|'
    r'everything|all\s*games|tosec|goodset|good[a-z]+\b)',
    re.IGNORECASE,
)


def _rom_files(files: list[dict]) -> list[dict]:
    # library holds the canonical ROM/disc/archive extension set (scheduler imports
    # it the same lazy way to avoid a service→router import at module load).
    from app.routers.library import ROM_EXTENSIONS
    return [f for f in files if Path(f.get("name", "")).suffix.lower() in ROM_EXTENSIONS]


def _disc_base(name: str) -> str:
    """Normalized base-title signature of a filename with any disc marker removed,
    so the discs of one game collapse to the same key."""
    stem = _DISC_RE.sub(" ", Path(name).stem)
    return " ".join(sorted(significant_terms(search_title(stem))))


def looks_like_pack(title: str) -> bool:
    """Heuristic: does this release title look like a multi-game pack / full set?"""
    return bool(_PACK_RE.search(title or ""))


def release_is_relevant(title: str, title_terms: set[str]) -> bool:
    """Does the release title plausibly name the wanted game? (same rule as the hunt)."""
    return title_is_relevant(title or "", title_terms)


def classify_files(files: list[dict], ra_stems: set[str], title_terms: set[str]) -> dict:
    """Classify a torrent's file list against the wanted game.

    Returns {kind, keep_indices, keep_names, reason} where kind is one of:
      single    — one matching ROM (download whole torrent / just that file)
      multidisc — several files, all discs of the one wanted game → keep all
      pack      — wanted file(s) sit among UNRELATED games → keep only the matches
      none      — nothing matches → skip this release
    """
    roms = _rom_files(files)
    if not roms:
        return {"kind": "none", "keep_indices": [], "keep_names": [], "reason": "no ROM-like files"}

    scored = [(f, _file_score(f.get("name", ""), ra_stems, title_terms)) for f in roms]
    matched = [f for f, s in scored if s > 0]
    if not matched:
        return {"kind": "none", "keep_indices": [], "keep_names": [], "reason": "no file matches the wanted game"}

    unmatched = [f for f, s in scored if s == 0]
    has_disc = any(_DISC_RE.search(f.get("name", "")) for f in matched)
    bases = {_disc_base(f.get("name", "")) for f in matched}

    if unmatched:
        # Matches live among other, unrelated games → it's a pack; keep only the matches.
        kind = "pack"
    elif len(matched) > 1 and has_disc and len(bases) == 1:
        kind = "multidisc"      # several discs of the one wanted game
    elif len(matched) == 1:
        kind = "single"
    else:
        # Several matching ROM files that are all the wanted game (multi-disc without a
        # clean marker, or region variants) and nothing unrelated — treat as one game.
        kind = "multidisc" if has_disc else "single"

    keep_indices = [f["index"] for f in matched if isinstance(f.get("index"), int)]
    keep_names = [f.get("name", "") for f in matched]
    return {"kind": kind, "keep_indices": keep_indices, "keep_names": keep_names,
            "reason": f"{len(matched)}/{len(roms)} ROM file(s) matched"}
