"""Phase 5: resilient resumable RA re-verify."""

import asyncio

import pytest
from sqlmodel import Session, select

from app.db.models import LibraryEntry
from app.services import settings as app_settings, ra_verify
from app.services.sources.errors import SourceRateLimitError


class FakeRA:
    results: dict = {}
    raise_rl: bool = False

    def __init__(self, *a, **k):
        pass

    async def lookup_hash(self, h):
        if FakeRA.raise_rl:
            raise SourceRateLimitError("429", retry_after=60)
        return FakeRA.results.get(h)


@pytest.fixture()
def seeded(fresh_engine, monkeypatch):
    monkeypatch.setattr("app.services.ra_verify.RAClient", FakeRA)
    FakeRA.results = {"h_match": {"ID": 42}}
    FakeRA.raise_rl = False
    with Session(fresh_engine) as s:
        app_settings.set(s, "ra_username", "u")
        app_settings.set(s, "ra_api_key", "k")
        s.add(LibraryEntry(game_title="A", system="NES", file_name="a", file_path="/a",
                           file_hash="h_match", ra_matched=False))
        s.add(LibraryEntry(game_title="B", system="NES", file_name="b", file_path="/b",
                           file_hash="h_miss", ra_matched=False))
        s.commit()
    return fresh_engine


def test_match_and_miss_then_resumes_clear(seeded):
    res = asyncio.run(ra_verify.run_pass())
    assert res["status"] == "ok"
    assert res["matched"] == 1
    assert res["checked"] == 2
    with Session(seeded) as s:
        rows = {e.game_title: e for e in s.exec(select(LibraryEntry)).all()}
        assert rows["A"].ra_matched is True and rows["A"].ra_game_id == 42
        assert rows["B"].ra_matched is False and rows["B"].hash_verified is True
        assert rows["A"].ra_checked_at is not None and rows["B"].ra_checked_at is not None
    # Second pass: both checked recently → nothing pending (passes terminate).
    res2 = asyncio.run(ra_verify.run_pass())
    assert res2["checked"] == 0


def test_rate_limit_persists_pause_and_resumes(seeded):
    FakeRA.raise_rl = True
    res = asyncio.run(ra_verify.run_pass())
    assert res["status"] == "rate_limited"
    with Session(seeded) as s:
        assert app_settings.get(s, "ra_verify_paused_until") != ""
    # While paused, the next pass is a no-op (honoured even across restarts).
    res2 = asyncio.run(ra_verify.run_pass())
    assert res2["status"] == "paused"


def test_no_credentials(fresh_engine, monkeypatch):
    monkeypatch.setattr("app.services.ra_verify.RAClient", FakeRA)
    res = asyncio.run(ra_verify.run_pass())
    assert res["status"] == "no_credentials"
