"""Reworked bulk verify: scope (filtered/entire) + skip/include RA-matched."""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import settings as app_settings


def _seed_creds_and_entries():
    with Session(engine) as s:
        app_settings.set(s, "ra_username", "u")
        app_settings.set(s, "ra_api_key", "k")
        s.add(LibraryEntry(game_title="A", system="NES", file_name="a.nes", file_path="/a.nes",
                           file_hash="h1", ra_matched=False))
        s.add(LibraryEntry(game_title="B", system="NES", file_name="b.nes", file_path="/b.nes",
                           file_hash="h2", ra_matched=True))   # already matched
        s.commit()


def test_verify_defaults_to_unmatched_entire_library(client, monkeypatch):
    captured = {}

    async def fake_do_verify(ids, u, k):
        captured["ids"] = list(ids)

    monkeypatch.setattr("app.routers.collection._do_verify", fake_do_verify)
    _seed_creds_and_entries()

    r = client.post("/collection/bulk/verify")
    assert r.status_code == 200
    assert "unmatched only" in r.text
    assert "entire library" in r.text
    assert len(captured["ids"]) == 1            # only the unmatched one (A)


def test_verify_include_matched(client, monkeypatch):
    captured = {}

    async def fake_do_verify(ids, u, k):
        captured["ids"] = list(ids)

    monkeypatch.setattr("app.routers.collection._do_verify", fake_do_verify)
    _seed_creds_and_entries()

    r = client.post("/collection/bulk/verify?include_matched=true")
    assert "incl. matched" in r.text
    assert len(captured["ids"]) == 2            # A and the already-matched B


def test_verify_scoped_to_filtered_ids(client, monkeypatch):
    captured = {}

    async def fake_do_verify(ids, u, k):
        captured["ids"] = list(ids)

    monkeypatch.setattr("app.routers.collection._do_verify", fake_do_verify)
    _seed_creds_and_entries()
    with Session(engine) as s:
        a = s.exec(__import__("sqlmodel").select(LibraryEntry).where(LibraryEntry.game_title == "A")).first()
        a_id = a.id

    r = client.post(f"/collection/bulk/verify?library_ids={a_id}")
    assert "filtered view" in r.text
    assert captured["ids"] == [a_id]


def test_verify_dropdown_renders(client):
    r = client.get("/collection")
    assert r.status_code == 200
    assert "Re-verify all (incl. matched)" in r.text     # the new scoped dropdown


def test_do_verify_commits_each_match_durably(fresh_engine, monkeypatch):
    """A match must be committed immediately (visible no_ra drop) and survive a
    later entry's failure — not buffered until one final commit at batch end."""
    import asyncio
    from app.routers.collection import _do_verify
    from app.services import ra_client

    with Session(engine) as s:
        hit = LibraryEntry(game_title="Hit", system="PlayStation", file_name="h.chd",
                           file_path="/h.chd", file_hash="good", ra_matched=False)
        boom = LibraryEntry(game_title="Boom", system="PlayStation", file_name="x.chd",
                            file_path="/x.chd", file_hash="boom", ra_matched=False)
        s.add(hit); s.add(boom); s.commit()
        id_hit, id_boom = hit.id, boom.id

    class FakeRA:
        def __init__(self, u, k): pass
        async def lookup_hash(self, h):
            if h == "good":
                return {"ID": 555}
            raise RuntimeError("network blew up")   # a later entry fails mid-batch

    monkeypatch.setattr(ra_client, "RAClient", FakeRA)
    asyncio.run(_do_verify([id_hit, id_boom], "u", "k"))

    with Session(engine) as s:
        a = s.get(LibraryEntry, id_hit)
        b = s.get(LibraryEntry, id_boom)
    assert a.ra_matched and a.ra_game_id == 555      # committed despite boom failing after
    assert a.ra_checked_at is not None               # stamped → leaves resumable work set
    assert not b.ra_matched                          # the failure didn't abort the batch
