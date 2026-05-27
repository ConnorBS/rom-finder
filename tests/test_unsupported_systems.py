"""Platforms RA can't verify (e.g. Nintendo 3DS) are shown as 'unsupported',
excluded from no_ra, and never sent to RA by the verify paths."""

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry
from app.db import repository
from app.services.ra_client import is_ra_unsupported, RA_UNSUPPORTED_SYSTEMS


def test_is_ra_unsupported():
    assert is_ra_unsupported("Nintendo 3DS")
    assert not is_ra_unsupported("PlayStation")
    assert "Nintendo 3DS" in RA_UNSUPPORTED_SYSTEMS


def _seed():
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Zelda ALBW", system="Nintendo 3DS",
                            file_name="z.3ds", file_path="/z.3ds", file_hash="h3ds", ra_matched=False))
        s.add(LibraryEntry(game_title="Crash", system="PlayStation",
                            file_name="c.chd", file_path="/c.chd", file_hash="hps1", ra_matched=False))
        s.commit()


def test_verify_work_set_excludes_unsupported(fresh_engine):
    _seed()
    with Session(engine) as s:
        work = repository.library_pending_ra_check(s, exclude_systems=RA_UNSUPPORTED_SYSTEMS)
        systems = {e.system for e in work}
    assert "PlayStation" in systems
    assert "Nintendo 3DS" not in systems        # 3DS never enters the verify queue


def test_collection_counts_split_unsupported(client):
    _seed()
    # no_ra count must NOT include the 3DS entry; it lands under "unsupported"
    r = client.get("/collection/counts")
    assert r.status_code == 200
    assert "1 no RA match" in r.text          # only the PlayStation one
    assert "1 unsupported" in r.text           # the 3DS one


def test_collection_unsupported_filter(client):
    _seed()
    r = client.get("/collection?status=unsupported")
    assert r.status_code == 200
    assert "Zelda ALBW" in r.text
    assert "Crash" not in r.text


def test_bulk_verify_skips_unsupported(client, monkeypatch):
    captured = {}

    async def fake_do_verify(ids, u, k):
        captured["ids"] = list(ids)

    monkeypatch.setattr("app.routers.collection._do_verify", fake_do_verify)
    with Session(engine) as s:
        from app.services import settings as app_settings
        app_settings.set(s, "ra_username", "u")
        app_settings.set(s, "ra_api_key", "k")
        s.commit()
    _seed()

    r = client.post("/collection/bulk/verify")
    assert r.status_code == 200
    with Session(engine) as s:
        ids_3ds = {e.id for e in s.exec(
            select(LibraryEntry).where(LibraryEntry.system == "Nintendo 3DS")
        ).all()}
    assert ids_3ds.isdisjoint(set(captured.get("ids", [])))   # 3DS not queued for RA
