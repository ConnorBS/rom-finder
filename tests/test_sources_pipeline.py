"""Phase 4: error taxonomy + name-aware archive member selection."""

import zipfile
from pathlib import Path

from app.services.sources.errors import (
    classify_status, SourceForbiddenError, SourceNotFoundError,
    SourceRateLimitError, SourceNetworkError,
)
from app.services.hasher import extract_rom_from_zip, _hash_from_archive, md5_file


def test_classify_status_maps_codes():
    assert isinstance(classify_status(403), SourceForbiddenError)
    assert isinstance(classify_status(404), SourceNotFoundError)
    assert isinstance(classify_status(500), SourceNetworkError)
    assert isinstance(classify_status(400), SourceNetworkError)
    rl = classify_status(429, retry_after=12.0)
    assert isinstance(rl, SourceRateLimitError)
    assert rl.retry_after == 12.0


def _make_multi_rom_zip(path: Path):
    # game_small is the name-match target; game_big is larger (the old heuristic).
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("game_small.nes", b"A" * 10)
        zf.writestr("game_big.nes", b"B" * 5000)


def test_extract_prefers_name_match_over_largest(tmp_path):
    zpath = tmp_path / "bundle.zip"
    _make_multi_rom_zip(zpath)
    out = extract_rom_from_zip(zpath, prefer_name="game_small.nes")
    assert out.name == "game_small.nes"        # picked the match, not the bigger file
    assert out.read_bytes() == b"A" * 10


def test_extract_falls_back_to_largest_without_match(tmp_path):
    zpath = tmp_path / "bundle.zip"
    _make_multi_rom_zip(zpath)
    out = extract_rom_from_zip(zpath)            # no prefer_name
    assert out.name == "game_big.nes"            # largest


def test_hash_from_archive_prefers_name_match(tmp_path):
    zpath = tmp_path / "bundle.zip"
    _make_multi_rom_zip(zpath)
    # Hash with a name preference → should equal MD5 of the small file's bytes.
    import tempfile, hashlib
    expected = hashlib.md5(b"A" * 10).hexdigest()
    got = _hash_from_archive(zpath, system="NES", prefer_name="game_small.nes")
    assert got == expected
