"""Collection 'Scan folder' — reports scope, imports new ROMs, and removes
entries whose file is gone from disk (with an unmounted-drive safety guard)."""

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import settings as app_settings


def test_scan_reports_scope_imports_new_and_removes_orphan(client, tmp_path):
    nes = tmp_path / "NES"
    nes.mkdir()
    (nes / "Real Game (USA).nes").write_bytes(b"rom")

    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        # An entry whose file no longer exists on disk.
        s.add(LibraryEntry(game_title="Gone", system="NES",
                           file_name="Gone.nes", file_path=str(nes / "Gone.nes")))
        s.commit()

    r = client.post("/collection/bulk/scan")
    assert r.status_code == 200
    assert "Scanned" in r.text              # scope feedback
    assert "1 imported" in r.text
    assert "1 removed" in r.text

    with Session(engine) as s:
        titles = {e.game_title for e in s.exec(select(LibraryEntry)).all()}
    assert any("Real Game" in t for t in titles)   # new file imported
    assert "Gone" not in titles                     # orphan removed


def test_scan_skips_removal_when_most_files_missing(client, tmp_path):
    # download_dir exists (so the scan runs) but is empty; many entries look gone.
    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        for i in range(8):
            s.add(LibraryEntry(game_title=f"G{i}", system="NES",
                               file_name=f"g{i}.nes", file_path=str(tmp_path / f"g{i}.nes")))
        s.commit()

    r = client.post("/collection/bulk/scan")
    assert r.status_code == 200
    assert "removed" not in r.text          # safety guard: no mass deletion
    with Session(engine) as s:
        assert len(s.exec(select(LibraryEntry)).all()) == 8
