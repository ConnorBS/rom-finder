"""File size shows in the library detail slide-over (FILE section + each 'Same content'
duplicate entry), and a download-approved entry is sized on creation so the size sort +
size display work without waiting for a rescan."""

from pathlib import Path

from sqlmodel import Session

from app.db.database import engine
from app.db.models import Download, DownloadStatus, LibraryEntry
from app.db.repository import create_library_entry_from_download


def test_detail_panel_shows_file_size(client):
    with Session(engine) as s:
        canon = LibraryEntry(game_title="Big Game", system="Wii", file_name="big.iso",
                             file_path="/roms/Wii/big.iso", file_size=12_582_912)  # 12.0 MB
        s.add(canon)
        s.commit()
        s.refresh(canon)
        cid = canon.id

    html = client.get(f"/library/{cid}/detail").text
    assert "Size" in html              # the new FILE-section row label
    assert "12.0 MB" in html           # formatted with the same fsize() as the cards


def test_small_rom_shows_kb_not_zero_mb(client):
    """A tiny NES ROM (tens of KB) must read as 'NN KB', not round to '0.0 MB'."""
    with Session(engine) as s:
        e = LibraryEntry(game_title="Duck Hunt", system="NES", file_name="dh.nes",
                         file_path="/roms/NES/dh.nes", file_size=24592)  # ~24 KB
        s.add(e)
        s.commit()
        s.refresh(e)
        eid = e.id

    html = client.get(f"/library/{eid}/detail").text
    assert "24 KB" in html
    assert "0.0 MB" not in html


def test_detail_panel_shows_size_for_each_duplicate(client):
    """The 'Same content' list shows each copy's size so the user can pick which to
    remove (e.g. a compressed .zip vs the larger raw dump)."""
    with Session(engine) as s:
        canon = LibraryEntry(game_title="Dup Game", system="NES", file_name="dup.nes",
                             file_path="/roms/NES/dup.nes", file_size=12_582_912)  # 12.0 MB
        s.add(canon)
        s.commit()
        s.refresh(canon)
        sib = LibraryEntry(game_title="Dup Game", system="NES", file_name="dup.zip",
                           file_path="/roms/NES/dup.zip", file_size=6_291_456,     # 6.0 MB
                           duplicate_of=canon.id)
        s.add(sib)
        s.commit()
        cid = canon.id

    html = client.get(f"/library/{cid}/detail").text
    assert "12.0 MB" in html and "6.0 MB" in html   # both copies' sizes in the dup group


def test_approved_entry_is_sized_on_creation(client, tmp_path):
    """create_library_entry_from_download stats the on-disk file so a fresh
    download/approval lands with file_size set (not 0 until the next scan)."""
    rom = tmp_path / "fresh.nes"
    rom.write_bytes(b"x" * 4096)
    with Session(engine) as s:
        d = Download(game_title="Fresh", system="NES", file_name="fresh.nes",
                     file_path=str(rom), source_url="http://x", source_id="archive_org",
                     status=DownloadStatus.pending_approval, file_hash="h", hash_verified=True,
                     ra_game_id=7)
        entry = create_library_entry_from_download(s, d, rom)
        assert entry.file_size == 4096
