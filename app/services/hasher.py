"""ROM hash utilities.

RetroAchievements identifies ROMs by MD5 hash.
Some systems require stripping headers before hashing (e.g. NES iNES header).
"""

import hashlib
import zipfile
from pathlib import Path

# Extensions treated as ROM files (used when picking from inside a zip)
_ROM_EXTENSIONS = {
    ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc",
    ".md", ".gen", ".smd", ".bin",
    ".iso", ".cue", ".chd",
    ".n64", ".z64", ".v64",
    ".nds", ".3ds",
    ".psp", ".cso",
    ".a26", ".lnx", ".pce", ".ws", ".wsc",
}


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


def extract_rom_from_zip(zip_path: Path) -> Path:
    """Extract the ROM file from a zip archive next to the zip, then delete the zip.

    If there are multiple files inside, picks the one with a known ROM extension
    (largest if ties). Returns the path to the extracted file.
    Raises ValueError if no ROM-like file is found inside.
    """
    dest_dir = zip_path.parent
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        rom_members = [m for m in members if Path(m.filename).suffix.lower() in _ROM_EXTENSIONS]
        if not rom_members:
            # Fall back to any non-directory member
            rom_members = [m for m in members if not m.filename.endswith("/")]
        if not rom_members:
            raise ValueError(f"No files found inside {zip_path.name}")
        # Pick the largest member (the ROM, not metadata)
        target = max(rom_members, key=lambda m: m.file_size)
        zf.extract(target, dest_dir)

    extracted = dest_dir / target.filename
    zip_path.unlink()
    return extracted


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
