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
    "Wii": 19,
    "Wii U": 20,
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
    "GameCube": 16,
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


_NODTOOL_BIN = "nodtool"
# GameCube(16)/Wii(19)/Wii U(20) — matched by resolved RA id (not exact system name)
# so folder-derived names like "Nintendo Wii" / "Nintendo Gamecube" still trigger the
# decompress step (get_ra_system_id resolves those via substring).
_GC_WII_RA_IDS = {16, 19, 20}
# Compressed disc formats nodtool decompresses to raw ISO (.iso is read by RAHasher).
_NODTOOL_FORMATS = {".rvz", ".wbfs", ".wia", ".gcz", ".ciso", ".nfs", ".tgc"}


def _rahasher_available() -> bool:
    return shutil.which(_RAHASHER_BIN) is not None


def _nodtool_available() -> bool:
    return shutil.which(_NODTOOL_BIN) is not None


def _convert_scratch_dir() -> Path:
    """Where temporary decompressed ISOs go — the review/staging area (`check_dir`),
    NEVER the curated ROM library (`download_dir`). Falls back to the OS temp dir if
    check_dir isn't configured. The dir is created if missing."""
    import tempfile
    check_dir = ""
    try:
        from sqlmodel import Session
        from app.db.database import engine
        from app.services import settings as app_settings
        with Session(engine) as s:
            check_dir = app_settings.get(s, "check_dir", "")
    except Exception:
        pass
    base = Path(check_dir) if check_dir else Path(tempfile.gettempdir())
    scratch = base / "_convert"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


async def _convert_to_iso(src: Path, system_name: str, ra_id) -> Path | None:
    """Decompress a GameCube/Wii disc image to a temporary raw ISO via nodtool, so
    RAHasher can hash it. Returns the temp ISO path (caller must delete it) or None.
    The temp ISO is written to the review/staging scratch dir (NOT the ROM library),
    and the conversion gets a generous timeout (Wii ISOs are large)."""
    dst = _convert_scratch_dir() / (src.stem + ".rahash.iso")
    try:
        proc = await asyncio.create_subprocess_exec(
            _NODTOOL_BIN, "convert", str(src), str(dst),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)  # 15 min
        if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            return dst
        _applog_fail(system_name, ra_id, src,
                     f"nodtool convert exit {proc.returncode}: {stderr.decode()[:200]}")
    except asyncio.TimeoutError:
        _applog_fail(system_name, ra_id, src, "nodtool convert timed out (15 min)")
    except Exception as exc:
        _applog_fail(system_name, ra_id, src, f"nodtool convert error: {exc}")
    try:
        dst.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _applog_fail(system_name: str, ra_id, rom_path: Path, reason: str) -> None:
    """Surface a RAHasher failure to the HTTP-visible app log (not just Docker
    stdout) — so an agent can diagnose why a system/format won't hash."""
    try:
        from app.services import logger as applog
        applog.warning(
            "hash",
            f"RAHasher could not hash '{rom_path.name}' (system {system_name}, id {ra_id}, "
            f"{rom_path.suffix or 'no-ext'}): {reason}",
            {"system": system_name, "ra_id": ra_id, "file": rom_path.name, "suffix": rom_path.suffix},
        )
    except Exception:
        pass


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


def disc_without_rahasher(system_name: str) -> bool:
    """True if this is a disc system that REQUIRES RAHasher and the binary is
    missing — a plain-MD5 fallback for these can never match RA's disc hash."""
    from app.services.hasher import DISC_SYSTEMS
    return system_name in DISC_SYSTEMS and not _rahasher_available()


async def ra_hash_or_fallback(rom_path: Path, system_name: str) -> tuple[str, bool]:
    """Compute the RA hash, falling back to plain MD5. Returns (hash, used_rahasher).

    When a disc-system ROM falls back to MD5 because RAHasher is unavailable, the
    hash is non-authoritative (cannot match RA) — we log a clear warning so it's
    diagnosable instead of silently surfacing as 'not in RA database'.
    """
    import asyncio
    from app.services.hasher import hash_rom

    ra_hash = await compute_ra_hash(rom_path, system_name)
    if ra_hash is not None:
        return ra_hash, True
    loop = asyncio.get_event_loop()
    fallback = await loop.run_in_executor(None, hash_rom, rom_path, system_name)
    if disc_without_rahasher(system_name):
        from app.services import logger as applog
        applog.warning(
            "hash",
            f"{system_name}: RAHasher unavailable — '{rom_path.name}' hashed as plain MD5, "
            "which cannot match RA's disc hash. Install RAHasher to verify disc systems.",
            {"system": system_name, "file": rom_path.name},
        )
    return fallback, False


async def compute_ra_hash(rom_path: Path, system_name: str) -> str | None:
    """Compute the RA hash using the RAHasher binary.

    Returns the hash string on success, or None if RAHasher is unavailable,
    the system ID is unknown, or execution fails (caller should fall back to
    the Python hasher).
    """
    ra_id = get_ra_system_id(system_name)
    if ra_id is None:
        logger.debug("No RA system ID for %r — skipping RAHasher", system_name)
        return None

    # GameCube/Wii compressed images (RVZ/WBFS/WIA/GCZ/CISO) — decompress to a
    # temporary raw ISO with nodtool first, then hash the ISO.
    hash_target = rom_path
    temp_iso: Path | None = None
    if (ra_id in _GC_WII_RA_IDS
            and rom_path.suffix.lower() in _NODTOOL_FORMATS
            and _nodtool_available()):
        temp_iso = await _convert_to_iso(rom_path, system_name, ra_id)
        if temp_iso is not None:
            hash_target = temp_iso

    try:
        # GameCube: the standalone RALibretro RAHasher's GameCube hash does NOT match
        # RA's server (it lags), so use a faithful Python port of rc_hash's GameCube
        # method (partition header + main.dol segments) on the (converted) ISO.
        if ra_id == 16:
            from app.services.hasher import md5_gamecube
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, md5_gamecube, hash_target)
            if not result:
                _applog_fail(system_name, ra_id, rom_path, "not a GameCube disc / parse failed")
            return result or None

        if not _rahasher_available():
            return None
        proc = await asyncio.create_subprocess_exec(
            _RAHASHER_BIN, str(ra_id), str(hash_target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        stderr_text = stderr.decode().strip()
        if proc.returncode != 0:
            print(f"[rahasher] exit {proc.returncode} for {rom_path.name}: {stderr_text}", flush=True)
            logger.warning("RAHasher exited %d for %s: %s", proc.returncode, rom_path.name, stderr_text)
            _applog_fail(system_name, ra_id, rom_path, f"exit {proc.returncode}: {stderr_text[:300]}")
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
        _applog_fail(system_name, ra_id, rom_path, f"unexpected output: {ra_hash[:80]!r}; stderr: {stderr_text[:200]}")
        return None
    except asyncio.TimeoutError:
        logger.warning("RAHasher timed out for %s", rom_path.name)
        return None
    except Exception as exc:
        logger.warning("RAHasher error for %s: %s", rom_path.name, exc)
        return None
    finally:
        if temp_iso is not None:
            try:
                temp_iso.unlink(missing_ok=True)
            except OSError:
                pass
