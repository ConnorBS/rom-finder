"""SNES/PC Engine fallback hashers must skip a 512-byte copier header exactly like
rcheevos rc_hash_snes (size % 0x2000 == 512) and rc_hash_pce (size & 512), so a
headered .smc/.pce hashes the same as the headerless ROM. Headerless ROMs (No-Intro)
must be unaffected."""

import hashlib

from app.services.hasher import md5_snes, md5_pce, hash_rom, _SYSTEM_HASHERS

_BODY = bytes(range(256)) * 32   # 8192 bytes — a multiple of 0x2000


def test_md5_snes_skips_copier_header(tmp_path):
    headerless = tmp_path / "g.sfc"; headerless.write_bytes(_BODY)
    headered = tmp_path / "g.smc"; headered.write_bytes(b"\x00" * 512 + _BODY)  # size%0x2000==512
    assert md5_snes(headerless) == hashlib.md5(_BODY).hexdigest()   # no false strip
    assert md5_snes(headered) == md5_snes(headerless)              # header ignored


def test_md5_pce_skips_copier_header(tmp_path):
    headerless = tmp_path / "g.pce"; headerless.write_bytes(_BODY)
    headered = tmp_path / "g2.pce"; headered.write_bytes(b"\x00" * 512 + _BODY)  # size & 512
    assert md5_pce(headerless) == hashlib.md5(_BODY).hexdigest()
    assert md5_pce(headered) == md5_pce(headerless)


def test_registered_and_dispatched(tmp_path):
    assert _SYSTEM_HASHERS.get("SNES") is md5_snes
    assert _SYSTEM_HASHERS.get("PC Engine / TurboGrafx-16") is md5_pce
    headered = tmp_path / "g.sfc"; headered.write_bytes(b"\x00" * 512 + _BODY)
    assert hash_rom(headered, "SNES") == hashlib.md5(_BODY).hexdigest()
