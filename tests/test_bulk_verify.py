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
