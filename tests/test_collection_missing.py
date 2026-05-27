"""Missing-ROM actions: permanent delete + move to wanted."""

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry, WantedGame, HuntStatus


def _add(**kw) -> int:
    with Session(engine) as s:
        e = LibraryEntry(**kw)
        s.add(e)
        s.commit()
        s.refresh(e)
        return e.id


def test_delete_missing_entry(client):
    eid = _add(game_title="X", system="NES", file_name="x.nes", file_path="/r/x.nes", missing=True)
    r = client.post(f"/collection/library/{eid}/delete")
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(LibraryEntry, eid) is None


def test_to_wanted_creates_hunting_and_removes_entry(client):
    eid = _add(game_title="Kirby", system="Wii", file_name="k.rvz", file_path="/r/k.rvz",
               ra_game_id=104, missing=True)
    r = client.post(f"/collection/library/{eid}/to-wanted")
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(LibraryEntry, eid) is None                 # moved out of library
        w = s.exec(select(WantedGame).where(WantedGame.ra_game_id == 104)).first()
        assert w is not None and w.status == HuntStatus.hunting  # now hunting


def test_to_wanted_requires_ra_id(client):
    eid = _add(game_title="NoRA", system="NES", file_name="n.nes", file_path="/r/n.nes", missing=True)
    r = client.post(f"/collection/library/{eid}/to-wanted")
    assert r.status_code == 200
    assert "can't add" in r.text.lower()
    with Session(engine) as s:
        assert s.get(LibraryEntry, eid) is not None             # kept (couldn't move)
