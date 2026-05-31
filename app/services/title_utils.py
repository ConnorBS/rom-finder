"""Utilities for cleaning ROM/game titles for source searching.

RA game titles often include disambiguation suffixes like "(PlayStation 2)" or
"· RetroAchievements", and No-Intro ROM filenames include region/language tags.
Neither of those appear in source (archive.org, Vimm) listing titles, so we
strip them before searching.
"""

import re

# Suffixes that appear in titles scraped/returned from the RA website
_RA_SUFFIXES = re.compile(
    r'\s*[·•\-]\s*RetroAchievements\s*$',
    re.IGNORECASE,
)

# Platform disambiguation in parens, e.g. "(PlayStation 2)" or "(NES)"
_PLATFORM_PARENS = re.compile(
    r'\s*\('
    r'(?:PlayStation(?: \d+)?|PSP|PS\d|Nintendo\s+(?:64|DS|DSi|Switch)|'
    r'SNES|NES|Famicom|Game\s*Boy(?:\s+(?:Advance|Color|Colour))?|'
    r'GameCube|Wii(?:\s*U)?|'
    r'Sega\s+(?:Genesis|Mega\s+Drive|CD|Saturn|32X|Dreamcast|Master\s+System)|'
    r'Mega\s+Drive|Saturn|Dreamcast|'
    r'Xbox(?:\s+(?:360|One|Series\s+[XS]))?|'
    r'Atari\s+\d{4}|Game\s+Gear|TurboGrafx|PC\s*Engine|'
    r'3DO|Jaguar|Lynx|Neo\s*Geo(?:\s+Pocket)?|WonderSwan|Virtual\s+Boy|'
    r'Arcade|MSX|Amstrad|Apple\s+II|PC-\w+'
    r')\)',
    re.IGNORECASE,
)

# No-Intro / Redump region and language tags in parens, e.g. "(USA)", "(En,Fr,De)"
_REGION_PARENS = re.compile(
    r'\s*\((?:'
    r'USA|Europe|Japan|World|Asia|Australia|Brazil|Korea|Spain|France|Germany|Italy|'
    r'Netherlands|Sweden|Norway|Denmark|Finland|China|Taiwan|'
    r'[A-Z][a-z](?:,[A-Z][a-z])+|[A-Z]{2,3}'  # language codes like "En,Fr,De" or "JPN"
    r')\)',
    re.IGNORECASE,
)

# Revision and version tags: (Rev 1), (v1.1), (Beta), (Proto), (Sample), (Demo)
_VERSION_PARENS = re.compile(
    r'\s*\((?:Rev\s*\d*[A-Z]?|v\d[\d.]*|Beta\s*\d*|Proto(?:type)?|Sample|Demo|Unl|Alt(?:\s*\d+)?)\)',
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    """Return a title stripped of RA suffixes and platform disambiguation."""
    t = _RA_SUFFIXES.sub('', title)
    t = _PLATFORM_PARENS.sub('', t)
    return t.strip()


def search_variations(title: str) -> list[str]:
    """Return ordered search query candidates from most to least specific.

    1. Cleaned title (RA suffixes + platform parens removed)
    2. Cleaned title with region/version tags also stripped
    3. Main title only (before ": " or " - " subtitle separator)

    Duplicates and blank strings are removed.
    """
    clean = clean_title(title)

    no_tags = _VERSION_PARENS.sub('', _REGION_PARENS.sub('', clean)).strip()
    # Also collapse leftover double-spaces
    no_tags = re.sub(r'  +', ' ', no_tags).strip()

    candidates = [clean, no_tags]

    # Subtitle variants for "no_tags" base
    for sep in (': ', ' - '):
        if sep in no_tags:
            main = no_tags.split(sep, 1)[0].strip()
            if len(main) >= 3:
                candidates.append(main)

    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Title relevance — keep a source's loose full-text search consistent with what
# the auto-hunt would actually accept ("search == hunt"). Both layers derive
# from the same significant-word set so they can never drift.
# ---------------------------------------------------------------------------

_STOP_WORDS = {"the", "of", "and", "a", "an", "to"}


def significant_terms(title: str) -> set[str]:
    """Significant lowercase word-tokens of a title (len > 2, non-stopword).
    The shared primitive behind both the auto-hunt file scorer
    (hunter._file_score) and the source-search relevance filter."""
    return {w for w in re.findall(r"[a-z0-9]+", title.lower())
            if len(w) > 2 and w not in _STOP_WORDS}


def title_is_relevant(candidate: str, want_terms: set[str]) -> bool:
    """True when `candidate` plausibly names the wanted game: every significant
    word of the wanted title appears in it, or (>= 3 words) all-but-one do.

    Mirrors hunter._file_score's title-fallback accept rule exactly, so the
    Wanted-page source search and the auto-hunt agree on which results are real
    matches — a loose site search (ROMsFun/Archive) surfaces sibling titles
    (a *different* 'Pajama Sam' game); those must not show as if downloadable."""
    if not want_terms:
        return True
    cand = candidate.lower()
    present = sum(1 for t in want_terms if t in cand)
    if present == len(want_terms):
        return True
    return len(want_terms) >= 3 and present >= len(want_terms) - 1


def _normalize_doubled_system(name: str) -> str:
    """Collapse a system name the Chrome extension doubled when scraping RA link
    text — exact repetition only ('WiiWii' -> 'Wii'). We deliberately do NOT try
    an abbrev+fullname / endswith heuristic: "Super Nintendo Entertainment System"
    ends with "Nintendo Entertainment System" and would be wrongly collapsed.
    The RA console id (resolved server-side) is the authoritative path; this is
    just a safe net for the name-only case."""
    if not name:
        return name
    s = name.strip()
    half = len(s) // 2
    if len(s) % 2 == 0 and half and s[:half] == s[half:]:
        return s[:half]
    return s


def canonical_system(name: str, system_id=None) -> str:
    """Resolve a clean, canonical RA system name. The RA console id is
    authoritative — when present and known, use SYSTEMS[id] regardless of the
    (possibly corrupted) scraped `name`. Otherwise normalize the name."""
    if system_id not in (None, ""):
        from app.services.ra_client import SYSTEMS
        try:
            canon = SYSTEMS.get(int(system_id))
        except (TypeError, ValueError):
            canon = None
        if canon:
            return canon
    return _normalize_doubled_system(name or "")


def stem_from_rom_name(rom_name: str) -> str:
    """Return a cleaned search query from a No-Intro/Redump ROM filename.

    e.g. "Burnout 3 - Takedown (USA).iso"  →  "Burnout 3 - Takedown"
    """
    if not rom_name:
        return ""
    import pathlib
    stem = pathlib.Path(rom_name).stem          # strip extension
    stem = _REGION_PARENS.sub('', stem)
    stem = _VERSION_PARENS.sub('', stem)
    return re.sub(r'  +', ' ', stem).strip()
