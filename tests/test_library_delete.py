"""Deleting a library entry — DB-only, and DB + ROM file (with disc-track cleanup and
a read-only-root guard) — plus the duplicate-group surfaced in the detail panel so the
user can confirm which files are duplicates of each other before deleting."""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import settings as app_settings


def _add(file_path, **kw):
    with Session(engine) as s:
        e = LibraryEntry(game_title=kw.get("title", "Game"), system=kw.get("system", "NES"),
                         file_name=kw["file_name"], file_path=str(file_path),
                         file_hash=kw.get("file_hash"), ra_game_id=kw.get("ra_game_id"),
                         ra_matched=kw.get("ra_matched", False), duplicate_of=kw.get("duplicate_of"))
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def test_delete_entry_keeps_file(client, tmp_path):
    rom = tmp_path / "Game.nes"; rom.write_bytes(b"x")
    eid = _add(rom, file_name="Game.nes")
    r = client.post(f"/collection/library/{eid}/delete")
    assert r.status_code == 200
    assert rom.exists()                                   # file untouched
    with Session(engine) as s:
        assert s.get(LibraryEntry, eid) is None           # entry gone


def test_delete_entry_and_file(client, tmp_path):
    rom = tmp_path / "Game.nes"; rom.write_bytes(b"x")
    eid = _add(rom, file_name="Game.nes")
    r = client.post(f"/collection/library/{eid}/delete?delete_file=true")
    assert r.status_code == 200
    assert not rom.exists()                               # file deleted
    with Session(engine) as s:
        assert s.get(LibraryEntry, eid) is None


def test_delete_cue_removes_sibling_tracks(client, tmp_path):
    cue = tmp_path / "Disc (USA).cue"; cue.write_text('FILE "Disc (USA).bin" BINARY\n')
    binf = tmp_path / "Disc (USA).bin"; binf.write_bytes(b"\x00" * 16)
    eid = _add(cue, file_name="Disc (USA).cue", system="PlayStation")
    client.post(f"/collection/library/{eid}/delete?delete_file=true")
    assert not cue.exists() and not binf.exists()         # descriptor + track both gone


def test_delete_file_refused_when_readonly(client, tmp_path):
    rom = tmp_path / "Game.nes"; rom.write_bytes(b"x")
    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        app_settings.set(s, "download_dir_readonly", "true")
        s.commit()
    eid = _add(rom, file_name="Game.nes")
    r = client.post(f"/collection/library/{eid}/delete?delete_file=true")
    assert "read-only" in r.text.lower()
    assert rom.exists()                                   # file kept
    with Session(engine) as s:
        assert s.get(LibraryEntry, eid) is not None       # entry kept too


def test_detail_panel_shows_duplicate_group(client, tmp_path):
    a = tmp_path / "Foo.cue"; a.write_text("x")
    b = tmp_path / "Foo.7z"; b.write_bytes(b"x")
    canon = _add(a, title="Foo", system="PlayStation", file_name="Foo.cue", file_hash="H", ra_matched=True)
    _add(b, title="Foo", system="PlayStation", file_name="Foo.7z", file_hash="H", duplicate_of=canon)
    r = client.get(f"/library/{canon}/detail")
    assert r.status_code == 200
    assert "Duplicates" in r.text and "Kept" in r.text
    assert "Foo.7z" in r.text                              # the dup copy is listed
