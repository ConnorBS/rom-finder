"""Arduboy hashing: RA reads the .hex as text and normalizes line endings, so a
CRLF dump and an LF dump of the same program must hash identically. Hashing raw
bytes made every CRLF repack miss RA. Verified against RA's accepted hash for
'Under the Tower (Erwin's Collection)'."""

import hashlib

from app.services.hasher import md5_arduboy, hash_rom, _SYSTEM_HASHERS


_HEX_BODY = b":100000000C9462000C9485000C94850088\n:00000001FF\n"


def test_crlf_and_lf_hash_identically(tmp_path):
    lf = tmp_path / "game.hex"; lf.write_bytes(_HEX_BODY)
    crlf = tmp_path / "game_crlf.hex"; crlf.write_bytes(_HEX_BODY.replace(b"\n", b"\r\n"))
    # Both line-ending variants of the same program → same RA hash.
    assert md5_arduboy(lf) == md5_arduboy(crlf)
    # And it equals the LF-normalized MD5 (what RA computes).
    assert md5_arduboy(crlf) == hashlib.md5(_HEX_BODY).hexdigest()
    # Raw-bytes MD5 of the CRLF file differs — the old (buggy) behavior.
    assert hashlib.md5(crlf.read_bytes()).hexdigest() != md5_arduboy(crlf)


def test_arduboy_trailing_newline_always_appended(tmp_path):
    # rc_hash_text appends '\n' after every line, so a file with no trailing
    # newline hashes the same as one with it (and as the CRLF variant).
    no_nl = tmp_path / "a.hex"; no_nl.write_bytes(b":00000001FF")
    with_nl = tmp_path / "b.hex"; with_nl.write_bytes(b":00000001FF\n")
    crlf = tmp_path / "c.hex"; crlf.write_bytes(b":00000001FF\r\n")
    assert md5_arduboy(no_nl) == md5_arduboy(with_nl) == md5_arduboy(crlf)
    assert md5_arduboy(no_nl) == hashlib.md5(b":00000001FF\n").hexdigest()


def test_arduboy_registered():
    assert _SYSTEM_HASHERS.get("Arduboy") is md5_arduboy


def test_hash_rom_uses_arduboy_normalizer(tmp_path):
    crlf = tmp_path / "g.hex"; crlf.write_bytes(_HEX_BODY.replace(b"\n", b"\r\n"))
    assert hash_rom(crlf, "Arduboy") == hashlib.md5(_HEX_BODY).hexdigest()
