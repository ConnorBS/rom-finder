"""Wii (and Wii U) are disc systems that MUST hash via RAHasher (= rc_hash) — plain
MD5 of the image never matches RA's Wii disc hash. Regression guard for the bug where
a correctly-downloaded Wii ROM (Kirby's Return to Dream Land) failed verification
because Wii wasn't mapped to RAHasher."""

from app.services.rahasher import get_ra_system_id, SYSTEM_NAME_TO_RA_ID
from app.services.hasher import DISC_SYSTEMS, _ROM_EXTENSIONS
from app.routers.library import ROM_EXTENSIONS


def test_wii_mapped_to_rahasher():
    assert get_ra_system_id("Wii") == 19          # RA console id for Wii
    assert get_ra_system_id("Wii U") == 20
    assert SYSTEM_NAME_TO_RA_ID["Wii"] == 19


def test_wii_is_a_disc_system():
    # So the disc-guard treats it correctly and it never silently plain-MD5s.
    assert "Wii" in DISC_SYSTEMS and "Wii U" in DISC_SYSTEMS


def test_wii_gc_formats_recognized():
    for ext in (".rvz", ".wbfs", ".wia", ".gcz"):
        assert ext in _ROM_EXTENSIONS          # hashing/extraction
        assert ext in ROM_EXTENSIONS           # scanning
