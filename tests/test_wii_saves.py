"""Wii save detection — keyed by **title ID low** (4 ASCII chars in the disc header), not
by filename, because Dolphin stores Wii saves under
    User/Wii/title/{type}/<hex(title_id_low)>/data/
not as per-game `.srm`/`.state` files."""

import json
from pathlib import Path

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import settings as app_settings
from app.services.saves import (
    _read_wii_title_id, _scan_wii_nand, scan_saves,
)


def _make_rvz(tmp_path: Path, name: str, tid: bytes) -> Path:
    """Synthetic .rvz: stuff the 4-char title ID at WIA/RVZ offset 0x58 (where the
    embedded disc-header copy starts: WIA_VERSION 0x48 + WIADisc preamble 0x10)."""
    p = tmp_path / name
    buf = bytearray(0x100)
    buf[0:4] = b"RVZ\x01"
    buf[0x58:0x5C] = tid
    p.write_bytes(bytes(buf))
    return p


def _make_iso(tmp_path: Path, name: str, tid: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(tid + b"\x00" * 60)
    return p


def test_read_title_id_iso(tmp_path):
    p = _make_iso(tmp_path, "Game.iso", b"RKME")
    assert _read_wii_title_id(p) == "RKME"


def test_read_title_id_rvz(tmp_path):
    p = _make_rvz(tmp_path, "Game.rvz", b"SX4E")
    assert _read_wii_title_id(p) == "SX4E"


def test_read_title_id_unknown_extension_returns_none(tmp_path):
    p = tmp_path / "Game.bin"; p.write_bytes(b"RKME" + b"\x00" * 60)
    assert _read_wii_title_id(p) is None       # not a recognized fast-path format


def test_scan_wii_nand_decodes_folder_hex(tmp_path):
    # Dolphin layout: /…/title/00010000/<8 hex of title_id_low>/data/banner.bin
    # 0x52,0x4B,0x4D,0x45 = "RKME" → folder name "524B4D45".
    base = tmp_path / "Dolphin" / "User" / "Wii" / "title" / "00010000" / "524B4D45" / "data"
    base.mkdir(parents=True)
    (base / "banner.bin").write_bytes(b"\x00" * 64)
    nand = _scan_wii_nand(tmp_path)
    assert "RKME" in nand
    assert len(nand["RKME"]) == 1


def test_scan_wii_nand_skips_empty_data_dir(tmp_path):
    # An installed title with no save (empty data/) shouldn't count.
    base = tmp_path / "Wii" / "title" / "00010000" / "524B4D45" / "data"
    base.mkdir(parents=True)
    assert _scan_wii_nand(tmp_path) == {}


def test_scan_saves_attributes_wii_nand_save_to_library_entry(client, tmp_path):
    rom = _make_rvz(tmp_path, "Kirby's Epic Yarn (USA).rvz", b"RKME")
    nand_data = tmp_path / "Dolphin" / "Wii" / "title" / "00010000" / "524B4D45" / "data"
    nand_data.mkdir(parents=True)
    (nand_data / "RVForest.dat").write_bytes(b"\x00" * 1024)

    with Session(engine) as s:
        app_settings.set(s, "saves_dir", str(tmp_path / "Dolphin"))
        app_settings.set(s, "download_dir", str(tmp_path))
        s.commit()
    with Session(engine) as s:
        e = LibraryEntry(game_title="Kirby's Epic Yarn (USA)", system="Wii",
                         file_name=rom.name, file_path=str(rom))
        s.add(e); s.commit(); s.refresh(e); eid = e.id

    with Session(engine) as s:
        scan_saves(s)
        e = s.get(LibraryEntry, eid)
        assert e.disc_id == "RKME"                  # lazy-populated from the ROM header
        assert e.save_count == 1
        saves = json.loads(e.save_files)
        assert saves[0]["kind"] == "wii-nand"
        assert "RKME" in saves[0]["name"]
    # READ-ONLY guarantee
    assert (nand_data / "RVForest.dat").exists()
