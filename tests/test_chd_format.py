"""CHD container-codec detection (services/chd_format) — pure header read, no chdman."""
from app.services.chd_format import read_chd_codecs, chd_status


def _chd_header(version: int, codecs: list[str]) -> bytes:
    """Minimal CHD header: 'MComprHD' + length + version + 4x FourCC compressors."""
    head = b"MComprHD"
    head += (124).to_bytes(4, "big")     # header length (offset 8)
    head += version.to_bytes(4, "big")   # version      (offset 12)
    comp = b""                            # compressors  (offset 16, 4x4 bytes)
    for c in (codecs + ["", "", "", ""])[:4]:
        comp += (c.encode("ascii") + b"\x00\x00\x00\x00")[:4]
    return head + comp + b"\x00" * 64


def test_cdzs_is_flagged(tmp_path):
    p = tmp_path / "g.chd"
    p.write_bytes(_chd_header(5, ["cdzs", "cdzl", "cdfl"]))
    assert chd_status(p) == "cdzs"


def test_zstd_dvd_is_flagged(tmp_path):
    p = tmp_path / "g.chd"
    p.write_bytes(_chd_header(5, ["zstd", "zlib"]))
    assert chd_status(p) == "zstd"


def test_safe_cd_codecs_ok(tmp_path):
    p = tmp_path / "g.chd"
    p.write_bytes(_chd_header(5, ["cdlz", "cdzl", "cdfl"]))
    assert chd_status(p) == "ok"
    assert read_chd_codecs(p) == ["cdlz", "cdzl", "cdfl"]


def test_pre_v5_treated_safe(tmp_path):
    # Pre-v5 CHDs predate Zstandard entirely; our reader returns [] → "ok".
    p = tmp_path / "g.chd"
    p.write_bytes(_chd_header(4, []))
    assert chd_status(p) == "ok"


def test_non_chd_returns_blank(tmp_path):
    p = tmp_path / "g.chd"
    p.write_bytes(b"NOTACHD!" + b"\x00" * 40)
    assert chd_status(p) == ""
    assert read_chd_codecs(p) is None


def test_missing_file_returns_blank(tmp_path):
    assert chd_status(tmp_path / "nope.chd") == ""
