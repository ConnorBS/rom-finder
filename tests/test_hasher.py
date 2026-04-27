"""Tests for ROM hash functions in app/services/hasher.py"""
import hashlib
import struct
import zipfile
import tempfile
from pathlib import Path

import pytest

from app.services.hasher import (
    md5_file,
    md5_nes,
    md5_fds,
    md5_n64,
    md5_lynx,
    md5_a7800,
    hash_rom,
    extract_rom_from_zip,
    _ROM_EXTENSIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tmp(data: bytes, suffix: str = ".bin") -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.flush()
    return Path(tmp.name)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------------------
# md5_file
# ---------------------------------------------------------------------------

def test_md5_file_basic():
    data = b"hello world"
    p = _write_tmp(data)
    assert md5_file(p) == _md5(data)


def test_md5_file_empty():
    p = _write_tmp(b"")
    assert md5_file(p) == _md5(b"")


# ---------------------------------------------------------------------------
# md5_nes
# ---------------------------------------------------------------------------

def test_md5_nes_with_ines_header():
    header = b"NES\x1a" + b"\x00" * 12  # 16-byte iNES header
    rom_data = b"\xAA" * 64
    p = _write_tmp(header + rom_data, ".nes")
    # Should skip the header
    assert md5_nes(p) == _md5(rom_data)


def test_md5_nes_without_header():
    data = b"\xBB" * 64
    p = _write_tmp(data, ".nes")
    # No iNES magic → hash whole file
    assert md5_nes(p) == _md5(data)


# ---------------------------------------------------------------------------
# md5_n64 — byte-order variants
# ---------------------------------------------------------------------------

def test_md5_n64_z64_no_swap():
    # z64 big-endian magic: 0x80371240 — no swap needed
    data = bytearray(b"\x80\x37\x12\x40" + b"\x00" * 60)
    p = _write_tmp(bytes(data), ".z64")
    assert md5_n64(p) == _md5(bytes(data))


def test_md5_n64_v64_byte_swapped():
    original = bytearray(b"\x80\x37\x12\x40" + b"\xAA\xBB" * 8)
    # Build v64: swap every two bytes
    v64 = bytearray(len(original))
    for i in range(0, len(original) - 1, 2):
        v64[i], v64[i + 1] = original[i + 1], original[i]
    v64[0:4] = b"\x37\x80\x40\x12"
    p = _write_tmp(bytes(v64), ".v64")
    # After swap-back it should match the z64 hash
    assert md5_n64(p) == _md5(bytes(original))


# ---------------------------------------------------------------------------
# hash_rom dispatch
# ---------------------------------------------------------------------------

def test_hash_rom_dispatches_nes():
    header = b"NES\x1a" + b"\x00" * 12
    rom = b"\xFF" * 32
    p = _write_tmp(header + rom, ".nes")
    assert hash_rom(p, "NES") == _md5(rom)


def test_hash_rom_unknown_system_falls_back_to_md5():
    data = b"generic rom data"
    p = _write_tmp(data, ".bin")
    assert hash_rom(p, "Unknown System") == _md5(data)


def test_hash_rom_empty_system_string():
    data = b"test"
    p = _write_tmp(data)
    assert hash_rom(p, "") == _md5(data)


# ---------------------------------------------------------------------------
# extract_rom_from_zip
# ---------------------------------------------------------------------------

def test_extract_rom_from_zip_finds_largest_rom():
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "game.zip"
        small_rom = b"\x00" * 100
        big_rom = b"\xFF" * 500
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "this is a readme")
            zf.writestr("game_small.sfc", small_rom)
            zf.writestr("game.sfc", big_rom)

        result = extract_rom_from_zip(zip_path)
        assert result.name == "game.sfc"
        assert result.read_bytes() == big_rom
        assert not zip_path.exists()  # zip deleted after extraction


def test_extract_rom_from_zip_no_rom_extensions_falls_back_to_largest():
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "archive.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file_a.dat", b"small")
            zf.writestr("file_b.dat", b"larger content here")

        result = extract_rom_from_zip(zip_path)
        assert result.name == "file_b.dat"


def test_extract_rom_from_zip_empty_raises():
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "empty.zip"
        with zipfile.ZipFile(zip_path, "w"):
            pass  # empty archive

        with pytest.raises(ValueError, match="No files found"):
            extract_rom_from_zip(zip_path)


# ---------------------------------------------------------------------------
# ROM extension set sanity
# ---------------------------------------------------------------------------

def test_rom_extensions_include_common_formats():
    for ext in (".nes", ".sfc", ".gba", ".n64", ".iso", ".chd", ".nds"):
        assert ext in _ROM_EXTENSIONS, f"Expected {ext} in ROM_EXTENSIONS"
