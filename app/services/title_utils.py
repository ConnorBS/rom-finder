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


def stem_from_rom_name(rom_name: str) -> str:
    """Return a cleaned search query from a No-Intro/Redump ROM filename.

    e.g. "Burnout 3 - Takedown (USA).iso"  →  "Burnout 3 - Takedown"
    """
    import pathlib
    stem = pathlib.Path(rom_name).stem          # strip extension
    stem = _REGION_PARENS.sub('', stem)
    stem = _VERSION_PARENS.sub('', stem)
    return re.sub(r'  +', ' ', stem).strip()
