"""Re-hash scoping — unmatched_only must touch only not-yet-matched entries
(so fixing stale disc hashes doesn't disturb already-matched cartridge games)."""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry


def test_rehash_unmatched_only_selects_unmatched(client, monkeypatch):
    captured = {}

    async def fake_do_rehash(ids):
        captured["ids"] = list(ids)

    monkeypatch.setattr("app.routers.collection._do_rehash", fake_do_rehash)
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Matched", system="PlayStation", file_name="m.chd",
                           file_path="/m.chd", file_hash="h1", ra_matched=True))
        s.add(LibraryEntry(game_title="Unmatched", system="PlayStation", file_name="u.chd",
                           file_path="/u.chd", file_hash="h2", ra_matched=False))
        s.commit()

    r = client.post("/collection/bulk/rehash", data={"unmatched_only": "true"})
    assert r.status_code == 200
    assert "no RA calls" in r.text
    assert len(captured["ids"]) == 1            # only the unmatched one
