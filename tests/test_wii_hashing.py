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


def test_folder_style_names_resolve():
    # Folder-derived system names (user names folders "Nintendo X") must still resolve.
    assert get_ra_system_id("Nintendo Wii") == 19
    assert get_ra_system_id("Nintendo Gamecube") == 80


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
