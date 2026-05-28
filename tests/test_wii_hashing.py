"""Wii (and Wii U) are disc systems that MUST hash via RAHasher (= rc_hash) — plain
MD5 of the image never matches RA's Wii disc hash. Regression guard for the bug where
a correctly-downloaded Wii ROM (Kirby's Return to Dream Land) failed verification
because Wii wasn't mapped to RAHasher."""

import asyncio

from app.services import rahasher
from app.services.rahasher import get_ra_system_id, SYSTEM_NAME_TO_RA_ID
from app.services.hasher import DISC_SYSTEMS, _ROM_EXTENSIONS
from app.routers.library import ROM_EXTENSIONS


class _FakeProc:
    returncode = 0

    def __init__(self, out=b"ad50325115bee56a6ec875fed32aa711"):
        self._out = out

    async def communicate(self):
        return (self._out, b"")


def test_wii_mapped_to_rahasher():
    assert get_ra_system_id("Wii") == 19          # RA console id for Wii
    assert get_ra_system_id("Wii U") == 20
    assert SYSTEM_NAME_TO_RA_ID["Wii"] == 19


def test_gamecube_ra_id_is_16():
    # RA's GameCube console is 16 (was wrongly mapped to 80 → never matched).
    assert get_ra_system_id("GameCube") == 16
    assert SYSTEM_NAME_TO_RA_ID["GameCube"] == 16


def test_md5_gamecube_hashes_header_plus_dol_segments(tmp_path):
    # Faithful rc_hash GameCube: hash partition header + non-empty main.dol segments,
    # in order. Synthetic disc validates the offsets/endianness/order of the port.
    import struct
    import hashlib as _hl
    from app.services.hasher import md5_gamecube
    size = 0x6000
    disc = bytearray(i % 251 for i in range(size))
    disc[0x1c:0x20] = b"\xc2\x33\x9f\x3d"                      # GameCube magic
    body, trailer = 0x40, 0x10
    disc[0x2440 + 0x14:0x2440 + 0x18] = struct.pack(">I", body)
    disc[0x2440 + 0x18:0x2440 + 0x1c] = struct.pack(">I", trailer)
    header_size = 0x2440 + 0x20 + body + trailer
    dol = 0x3000
    disc[0x420:0x424] = struct.pack(">I", dol)                 # main.dol offset
    disc[dol:dol + 0xD8] = bytes(0xD8)                         # clear DOL header
    disc[dol + 0x00:dol + 0x04] = struct.pack(">I", 0x4000)    # code seg 0 offset
    disc[dol + 0x90:dol + 0x94] = struct.pack(">I", 0x80)      # code seg 0 size
    disc[dol + 0x1c:dol + 0x20] = struct.pack(">I", 0x5000)    # data seg (ix 7) offset
    disc[dol + 0xAC:dol + 0xB0] = struct.pack(">I", 0x40)      # data seg (ix 7) size
    f = tmp_path / "game.iso"; f.write_bytes(bytes(disc))

    exp = _hl.md5()
    exp.update(bytes(disc[0:header_size]))
    exp.update(bytes(disc[0x4000:0x4000 + 0x80]))
    exp.update(bytes(disc[0x5000:0x5000 + 0x40]))
    assert md5_gamecube(f) == exp.hexdigest()


def test_md5_gamecube_rejects_non_gamecube(tmp_path):
    from app.services.hasher import md5_gamecube
    f = tmp_path / "notgc.iso"; f.write_bytes(b"\x00" * 0x3000)   # no magic word
    assert md5_gamecube(f) == ""


def test_folder_style_names_resolve():
    # Folder-derived system names (user names folders "Nintendo X") must still resolve.
    assert get_ra_system_id("Nintendo Wii") == 19
    assert get_ra_system_id("Nintendo Gamecube") == 16


def test_wad_is_a_rom_extension():
    assert ".wad" in _ROM_EXTENSIONS and ".wad" in ROM_EXTENSIONS


def test_wii_is_a_disc_system():
    # So the disc-guard treats it correctly and it never silently plain-MD5s.
    assert "Wii" in DISC_SYSTEMS and "Wii U" in DISC_SYSTEMS


def test_wii_gc_formats_recognized():
    for ext in (".rvz", ".wbfs", ".wia", ".gcz"):
        assert ext in _ROM_EXTENSIONS          # hashing/extraction
        assert ext in ROM_EXTENSIONS           # scanning


def test_rvz_converted_to_iso_before_rahasher(monkeypatch, tmp_path):
    # A "Nintendo Wii"-folder .rvz (non-canonical system name) must still convert —
    # the trigger is the resolved RA id, not the exact system string.
    rvz = tmp_path / "Kirby.rvz"; rvz.write_bytes(b"rvz")
    iso = tmp_path / "Kirby.rahash.iso"; iso.write_bytes(b"iso")
    monkeypatch.setattr(rahasher, "_rahasher_available", lambda: True)
    monkeypatch.setattr(rahasher, "_nodtool_available", lambda: True)
    conv = {}

    async def fake_convert(src, system_name, ra_id):
        conv["src"] = src
        return iso

    monkeypatch.setattr(rahasher, "_convert_to_iso", fake_convert)
    cap = {}

    async def fake_exec(*args, **kw):
        cap["args"] = args
        return _FakeProc()

    monkeypatch.setattr(rahasher.asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(rahasher.compute_ra_hash(rvz, "Nintendo Wii"))
    assert out == "ad50325115bee56a6ec875fed32aa711"
    assert conv["src"] == rvz                       # conversion attempted on the .rvz
    assert str(iso) in cap["args"]                  # RAHasher hashed the ISO, not the .rvz
    assert str(rvz) not in cap["args"]


def test_convert_scratch_dir_is_check_dir_not_rom_library(fresh_engine, tmp_path):
    # Temp ISOs must land in the review/staging area, never the curated ROM library.
    from sqlmodel import Session
    from app.db.database import engine
    from app.services import settings as app_settings
    with Session(engine) as s:
        app_settings.set(s, "check_dir", str(tmp_path))
        s.commit()
    d = rahasher._convert_scratch_dir()
    assert d == tmp_path / "_convert"
    assert d.exists()


def test_plain_iso_not_converted(monkeypatch, tmp_path):
    iso = tmp_path / "Game.iso"; iso.write_bytes(b"iso")
    monkeypatch.setattr(rahasher, "_rahasher_available", lambda: True)
    monkeypatch.setattr(rahasher, "_nodtool_available", lambda: True)
    called = {"n": 0}

    async def fake_convert(*a):
        called["n"] += 1
        return None

    monkeypatch.setattr(rahasher, "_convert_to_iso", fake_convert)

    async def fake_exec(*args, **kw):
        return _FakeProc()

    monkeypatch.setattr(rahasher.asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(rahasher.compute_ra_hash(iso, "Wii"))
    assert out == "ad50325115bee56a6ec875fed32aa711"
    assert called["n"] == 0                         # raw .iso read directly by RAHasher
