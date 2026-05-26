"""Phase 6: RAHasher disc-system guard."""

import asyncio
import hashlib

from app.services import rahasher


def test_disc_without_rahasher_detection():
    # RAHasher binary is not on PATH in the test environment.
    assert rahasher.disc_without_rahasher("PlayStation") is True
    assert rahasher.disc_without_rahasher("Saturn") is True
    assert rahasher.disc_without_rahasher("NES") is False        # cartridge — MD5 is fine
    assert rahasher.disc_without_rahasher("") is False


def test_ra_hash_or_fallback_falls_back_to_md5(tmp_path):
    f = tmp_path / "rom.bin"
    f.write_bytes(b"hello world")
    h, used_rahasher = asyncio.run(rahasher.ra_hash_or_fallback(f, ""))
    assert used_rahasher is False                                 # no RAHasher in test env
    assert h == hashlib.md5(b"hello world").hexdigest()
