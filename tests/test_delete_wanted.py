"""Deleting an owned ROM also clears its Wanted record — but only once no owned copy
(duplicate / subset) still satisfies that Wanted."""
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry, WantedGame, HuntStatus


def test_bulk_delete_clears_wanted_when_last_copy_gone(client):
    with Session(engine) as s:
        s.add(WantedGame(game_title="Solo", system="NES", ra_game_id=111, status=HuntStatus.hunting))
        s.add(LibraryEntry(game_title="Solo", system="NES", file_name="s.nes",
                           file_path="/s.nes", ra_game_id=111))
        s.commit()
        lid = s.exec(select(LibraryEntry).where(LibraryEntry.ra_game_id == 111)).first().id

    r = client.post("/collection/bulk/delete", data={"library_ids": str(lid)})
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.exec(select(LibraryEntry).where(LibraryEntry.ra_game_id == 111)).first() is None
        assert s.exec(select(WantedGame).where(WantedGame.ra_game_id == 111)).first() is None  # cleared


def test_bulk_delete_keeps_wanted_when_duplicate_remains(client):
    with Session(engine) as s:
        s.add(WantedGame(game_title="Dup", system="NES", ra_game_id=222, status=HuntStatus.hunting))
        s.add(LibraryEntry(game_title="Dup", system="NES", file_name="d1.nes",
                           file_path="/d1.nes", ra_game_id=222))
        s.add(LibraryEntry(game_title="Dup", system="NES", file_name="d2.nes",
                           file_path="/d2.nes", ra_game_id=222))
        s.commit()
        first = s.exec(select(LibraryEntry).where(LibraryEntry.file_path == "/d1.nes")).first().id

    r = client.post("/collection/bulk/delete", data={"library_ids": str(first)})
    assert r.status_code == 200
    with Session(engine) as s:
        assert len(s.exec(select(LibraryEntry).where(LibraryEntry.ra_game_id == 222)).all()) == 1
        assert s.exec(select(WantedGame).where(WantedGame.ra_game_id == 222)).first() is not None  # kept
