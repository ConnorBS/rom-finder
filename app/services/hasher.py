"""ROM hash utilities.

RetroAchievements identifies ROMs by an "RA hash" computed per-platform.
For most cartridge systems the RA hash is an MD5 of the raw ROM data, sometimes
with a platform-specific header stripped first.  Disc-based systems (PlayStation,
Saturn, Dreamcast, etc.) require the external RAHasher binary — those are handled
by rahasher.py which is tried first in the download flow.

RA hash algorithm sources:
  https://github.com/RetroAchievements/rcheevos/blob/master/src/rhash/
"""

import hashlib
import struct
import zipfile
from pathlib import Path


# Extensions treated as ROM files when extracting archives
_ROM_EXTENSIONS = {
    ".nes", ".fds",                                      # Nintendo
    ".sfc", ".smc",                                      # SNES
    ".gba", ".gb", ".gbc",                               # Game Boy
    ".n64", ".z64", ".v64",                              # N64
    ".nds", ".3ds",                                      # DS
    ".md", ".gen", ".smd", ".32x",                       # Sega
    ".sms", ".gg",                                       # Master System / Game Gear
    ".pce",                                              # PC Engine
    ".ngp", ".ngc",                                      # Neo Geo Pocket
    ".ws", ".wsc",                                       # WonderSwan
    ".lnx",                                              # Atari Lynx
    ".a26", ".a78",                                      # Atari 2600 / 7800
    ".col",                                              # Colecovision
    ".int",                                              # Intellivision
    ".vb",                                               # Virtual Boy
    ".min",                                              # Pokemon Mini
    ".bin", ".rom",                                      # Generic cartridge
    ".iso", ".cue", ".chd", ".bin",                      # Disc images
    ".psp", ".cso",                                      # PSP
}


# ---------------------------------------------------------------------------
# Cartridge system hashers
# ---------------------------------------------------------------------------

def md5_file(path: Path) -> str:
    """MD5 of the raw file bytes — correct for most cartridge systems."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_nes(path: Path) -> str:
    """NES: MD5 of ROM data, skip 16-byte iNES header if magic 'NES\\x1a' present."""
    with open(path, "rb") as f:
        header = f.read(16)
        data = f.read() if header[:4] == b"NES\x1a" else header + f.read()
    return hashlib.md5(data).hexdigest()


def md5_fds(path: Path) -> str:
    """Famicom Disk System: skip 16-byte FDS header if magic '\\x01*NINTENDO-HVC*' present."""
    with open(path, "rb") as f:
        header = f.read(16)
        # FDS header magic: 0x01 followed by ASCII "*NINTENDO-HVC*"
        data = f.read() if header[0:1] == b"\x01" and b"NINTENDO-HVC" in header else header + f.read()
    return hashlib.md5(data).hexdigest()


def md5_lynx(path: Path) -> str:
    """Atari Lynx: skip 64-byte LYNX header if 'LYNX' magic present at offset 0."""
    with open(path, "rb") as f:
        magic = f.read(4)
        f.seek(0)
        if magic == b"LYNX":
            f.read(64)  # skip header
            data = f.read()
        else:
            data = f.read()
    return hashlib.md5(data).hexdigest()


def md5_a7800(path: Path) -> str:
    """Atari 7800: skip 128-byte A7800 header if magic 'ATARI7800' present at offset 1."""
    with open(path, "rb") as f:
        header = f.read(128)
        if header[1:10] == b"ATARI7800":
            data = f.read()
        else:
            f.seek(0)
            data = f.read()
    return hashlib.md5(data).hexdigest()


def md5_n64(path: Path) -> str:
    """N64: MD5 of ROM data, byte-swap v64 (byte-swapped) or n64 (word-swapped) to z64 first."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    if len(data) >= 4:
        # Detect byte order from magic bytes
        if data[0:4] == b"\x37\x80\x40\x12":  # v64 (byte-swapped)
            for i in range(0, len(data) - 1, 2):
                data[i], data[i + 1] = data[i + 1], data[i]
        elif data[0:4] == b"\x40\x12\x37\x80":  # n64 (word-swapped)
            for i in range(0, len(data) - 3, 4):
                data[i:i + 4] = data[i + 3:i - 1:-1]
        # z64 (big-endian) needs no conversion
    return hashlib.md5(bytes(data)).hexdigest()


# ---------------------------------------------------------------------------
# System name → hasher mapping
# (keys should match the system strings stored in Download.system)
# ---------------------------------------------------------------------------

_SYSTEM_HASHERS = {
    # NES / Famicom
    "NES": md5_nes,
    "Nintendo Entertainment System": md5_nes,
    "Famicom": md5_nes,

    # FDS
    "FDS": md5_fds,
    "Famicom Disk System": md5_fds,

    # N64
    "Nintendo 64": md5_n64,
    "N64": md5_n64,

    # Atari Lynx
    "Atari Lynx": md5_lynx,
    "Lynx": md5_lynx,

    # Atari 7800
    "Atari 7800": md5_a7800,

    # All other cartridge systems fall through to md5_file (the default)
}

# Disc-based systems that require RAHasher — Python MD5 will NOT produce a
# correct RA hash for these.  The download flow tries RAHasher first; these
# are listed here so we can surface a warning when it isn't available.
DISC_SYSTEMS = {
    "PlayStation",
    "PlayStation 2",
    "PlayStation Portable",
    "Sega CD",
    "Saturn",
    "Dreamcast",
    "3DO Interactive Multiplayer",
    "PC Engine CD",
    "PC-FX",
    "TurboGrafx CD",
    "GameCube",
    "Atari Jaguar CD",
}


def hash_rom(path: Path, system: str = "") -> str:
    """Hash a ROM using the RA algorithm for its system.

    For disc-based systems this returns a plain MD5 which will NOT match RA's
    hash — rahasher.py should be tried first for those.
    """
    hasher = _SYSTEM_HASHERS.get(system, md5_file)
    return hasher(path)


def verify_hash(path: Path, expected: str, system: str = "") -> bool:
    return hash_rom(path, system).lower() == expected.lower()


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------

def extract_rom_from_zip(zip_path: Path) -> Path:
    """Extract the ROM file from a zip archive next to the zip, then delete the zip."""
    dest_dir = zip_path.parent
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        rom_members = [m for m in members if Path(m.filename).suffix.lower() in _ROM_EXTENSIONS]
        if not rom_members:
            rom_members = [m for m in members if not m.filename.endswith("/")]
        if not rom_members:
            raise ValueError(f"No files found inside {zip_path.name}")
        target = max(rom_members, key=lambda m: m.file_size)
        zf.extract(target, dest_dir)
    extracted = dest_dir / target.filename
    zip_path.unlink()
    return extracted
