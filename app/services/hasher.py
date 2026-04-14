"""ROM hash utilities.

RetroAchievements identifies ROMs by MD5 hash.
Some systems require stripping headers before hashing (e.g. NES iNES header).
"""

import hashlib
from pathlib import Path


def md5_file(path: Path) -> str:
    """Compute the MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_nes(path: Path) -> str:
    """MD5 of an NES ROM, skipping the 16-byte iNES header if present."""
    with open(path, "rb") as f:
        header = f.read(16)
        data = f.read() if header[:4] == b"NES\x1a" else header + f.read()
    return hashlib.md5(data).hexdigest()


# Systems that need special hash handling
_SYSTEM_HASHERS = {
    "NES": md5_nes,
    "Nintendo Entertainment System": md5_nes,
}


def hash_rom(path: Path, system: str = "") -> str:
    """Hash a ROM using the appropriate method for its system."""
    hasher = _SYSTEM_HASHERS.get(system, md5_file)
    return hasher(path)


def verify_hash(path: Path, expected: str, system: str = "") -> bool:
    """Return True if the file's hash matches expected (case-insensitive)."""
    return hash_rom(path, system).lower() == expected.lower()
