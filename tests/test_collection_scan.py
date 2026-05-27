"""Collection 'Scan folder' — reports scope, imports new ROMs, and flags entries
whose file left disk as missing (not deleted), resurrecting them if they return."""

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import settings as app_settings


def test_scan_reports_scope_imports_new_and_flags_missing(client, tmp_path):
    nes = tmp_path / "NES"
    nes.mkdir()
    (nes / "Real Game (USA).nes").write_bytes(b"rom")

    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        s.add(LibraryEntry(game_title="Gone", system="NES",
                           file_name="Gone.nes", file_path=str(nes / "Gone.nes")))
        s.commit()

    r = client.post("/collection/bulk/scan")
    assert r.status_code == 200
    assert "Scanned" in r.text
    assert "1 imported" in r.text
    assert "marked missing" in r.text

    with Session(engine) as s:
        rows = {e.game_title: e for e in s.exec(select(LibraryEntry)).all()}
    assert any("Real Game" in t for t in rows)
    assert "Gone" in rows and rows["Gone"].missing is True    # flagged, NOT deleted


def test_scan_resurrects_returned_file(client, tmp_path):
    nes = tmp_path / "NES"
    nes.mkdir()
    f = nes / "Back (USA).nes"
    f.write_bytes(b"rom")
    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        s.add(LibraryEntry(game_title="Back", system="NES", file_name="Back (USA).nes",
                           file_path=str(f), missing=True))   # flagged missing, but file is back
        s.commit()

    r = client.post("/collection/bulk/scan")
    assert "restored" in r.text
    with Session(engine) as s:
        e = s.exec(select(LibraryEntry).where(LibraryEntry.game_title == "Back")).first()
    assert e.missing is False


def test_scan_skips_flagging_when_most_files_missing(client, tmp_path):
    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        for i in range(8):
            s.add(LibraryEntry(game_title=f"G{i}", system="NES",
                               file_name=f"g{i}.nes", file_path=str(tmp_path / f"g{i}.nes")))
        s.commit()

    r = client.post("/collection/bulk/scan")
    assert r.status_code == 200
    assert "marked missing" not in r.text          # safety guard: no mass flagging
    with Session(engine) as s:
        entries = s.exec(select(LibraryEntry)).all()
    assert len(entries) == 8
    assert all(not e.missing for e in entries)
