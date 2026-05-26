"""RAHasher integration.

RAHasher is the official RetroAchievements hashing tool that implements the
correct per-platform RA hash algorithm for every supported system, including
disc-based platforms (PlayStation, Saturn, Dreamcast, etc.) that cannot be
hashed with a simple MD5.

Binary: https://github.com/RetroAchievements/RAHasher/releases
The Dockerfile downloads it to /usr/local/bin/RAHasher at build time.

Usage in the download flow:
    from app.services.rahasher import compute_ra_hash
    ra_hash = await compute_ra_hash(rom_path, system_name)
    if ra_hash is None:
        ra_hash = hash_rom(rom_path, system_name)  # Python fallback
"""

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Maps RA system display names (from ra_client.SYSTEMS) to RA system IDs.
# RAHasher uses the numeric ID to apply the correct hashing algorithm.
SYSTEM_NAME_TO_RA_ID: dict[str, int] = {
    "Sega Genesis / Mega Drive": 1,
    "Nintendo 64": 2,
    "SNES": 3,
    "Super Nintendo Entertainment System": 3,
    "Game Boy": 4,
    "Game Boy Advance": 5,
    "Game Boy Color": 6,
    "NES": 7,
    "Nintendo Entertainment System": 7,
    "Famicom": 7,
    "PC Engine / TurboGrafx-16": 8,
    "TurboGrafx-16": 8,
    "Sega CD": 9,
    "Sega 32X": 10,
    "Master System": 11,
    "PlayStation": 12,
    "Atari Lynx": 13,
    "Neo Geo Pocket": 14,
    "Game Gear": 15,
    "Atari Jaguar": 17,
    "Nintendo DS": 18,
    "PlayStation 2": 21,
    "Magnavox Odyssey 2": 23,
    "Pokemon Mini": 24,
    "Atari 2600": 25,
    "Arcade": 27,
    "Virtual Boy": 28,
    "MSX": 29,
    "SG-1000": 33,
    "Amstrad CPC": 37,
    "Apple II": 38,
    "Saturn": 39,
    "Dreamcast": 40,
    "PlayStation Portable": 41,
    "3DO Interactive Multiplayer": 43,
    "ColecoVision": 44,
    "Intellivision": 45,
    "Vectrex": 46,
    "PC-8000/8800": 47,
    "PC-FX": 49,
    "Atari 7800": 51,
    "WonderSwan": 53,
    "Fairchild Channel F": 56,
    "Philips CD-i": 57,
    "PC Engine CD": 76,
    "Nintendo DSi": 78,
    "GameCube": 80,
}

# Additional aliases for common abbreviated names
_ALIASES: dict[str, int] = {
    "Genesis": 1,
    "Mega Drive": 1,
    "N64": 2,
    "Super Nintendo": 3,
    "SFC": 3,
    "GBA": 5,
    "GBC": 6,
    "GBColor": 6,
    "GB": 4,
    "Game Boy Colour": 6,
    "NDS": 18,
    "DS": 18,
    "PSP": 41,
    "PS1": 12,
    "PS2": 21,
    "PS3": None,  # Not supported by RA
    "Sega Saturn": 39,
    "TG16": 8,
    "PCE": 8,
    "NGP": 14,
    "WS": 53,
    "WSC": 53,
    "Colecovision": 44,
    "SMS": 11,
    "Master System / Mark III": 11,
    "Jaguar": 17,
}

_FULL_MAP = {**SYSTEM_NAME_TO_RA_ID, **_ALIASES}

_RAHASHER_BIN = "RAHasher"  # expected on PATH


def _rahasher_available() -> bool:
    return shutil.which(_RAHASHER_BIN) is not None


def rahasher_status() -> dict:
    """Availability of the RAHasher binary — for /api/status and the disc guard.

    Disc-based systems (PlayStation, Saturn, Dreamcast, CHD, etc.) can only be
    hashed correctly by this binary; without it they fall back to plain MD5 and
    can never match RA's database.
    """
    path = shutil.which(_RAHASHER_BIN)
    return {"available": path is not None, "path": path, "bin": _RAHASHER_BIN}


def get_ra_system_id(system_name: str) -> int | None:
    """Return the RA numeric system ID for a system name, or None if unknown."""
    if not system_name:
        return None
    # Exact match first
    ra_id = _FULL_MAP.get(system_name)
    if ra_id is not None:
        return ra_id
    # Case-insensitive fallback
    lower = system_name.lower()
    for key, val in _FULL_MAP.items():
        if key.lower() == lower:
            return val
    # Substring match (e.g. "Nintendo Entertainment System (NES)" → 7)
    for key, val in _FULL_MAP.items():
        if key.lower() in lower or lower in key.lower():
            return val
    return None


async def compute_ra_hash(rom_path: Path, system_name: str) -> str | None:
    """Compute the RA hash using the RAHasher binary.

    Returns the hash string on success, or None if RAHasher is unavailable,
    the system ID is unknown, or execution fails (caller should fall back to
    the Python hasher).
    """
    if not _rahasher_available():
        return None

    ra_id = get_ra_system_id(system_name)
    if ra_id is None:
        logger.debug("No RA system ID for %r — skipping RAHasher", system_name)
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            _RAHASHER_BIN, str(ra_id), str(rom_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        stderr_text = stderr.decode().strip()
        if proc.returncode != 0:
            print(f"[rahasher] exit {proc.returncode} for {rom_path.name}: {stderr_text}", flush=True)
            logger.warning("RAHasher exited %d for %s: %s", proc.returncode, rom_path.name, stderr_text)
            return None
        if stderr_text:
            # Log any stderr even on success — CHD/format warnings appear here
            print(f"[rahasher] stderr for {rom_path.name}: {stderr_text}", flush=True)
        ra_hash = stdout.decode().strip()
        print(f"[rahasher] stdout for {rom_path.name}: {ra_hash!r}", flush=True)
        if len(ra_hash) == 32:  # valid MD5-length hex string
            return ra_hash
        print(f"[rahasher] unexpected output for {rom_path.name}: {ra_hash!r}", flush=True)
        logger.warning("RAHasher returned unexpected output for %s: %r", rom_path.name, ra_hash)
        return None
    except asyncio.TimeoutError:
        logger.warning("RAHasher timed out for %s", rom_path.name)
        return None
    except Exception as exc:
        logger.warning("RAHasher error for %s: %s", rom_path.name, exc)
        return None
