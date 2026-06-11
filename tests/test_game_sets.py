"""RA V2 set-aware multiset (alongside the V1 subset cache).

Parser classifies sets (core excluded; bonus/challenge = base-compatible; specialty/
exclusive = patch-required with the game's patchUrl), and refresh_game_sets caches them
per owned game. Network mocked.
"""
import asyncio

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry, RAGameSet
from app.services.ra_client_v2 import RAClientV2


def _game_payload():
    return {
        "data": {"type": "games", "id": "10003", "attributes": {"title": "Super Mario 64"}},
        "included": [
            {"type": "achievement-sets", "id": "100", "attributes": {"title": "Core", "types": ["core"], "pointsTotal": 400}},
            {"type": "achievement-sets", "id": "9282", "attributes": {"title": "Coin Collector", "types": ["bonus"], "pointsTotal": 100}},
            {"type": "achievement-sets", "id": "9999", "attributes": {"title": "Ultimate", "types": ["specialty"], "pointsTotal": 50}},
            {"type": "game-hashes", "id": "1", "attributes": {"compatibility": "compatible", "patchUrl": None}},
            {"type": "game-hashes", "id": "2", "attributes": {"compatibility": "patch-required", "patchUrl": "https://ra/patch/x.zip"}},
        ],
    }


def test_sets_from_game_classifies_and_excludes_core():
    sets = RAClientV2.sets_from_game(_game_payload())
    by_title = {s["title"]: s for s in sets}
    assert "Core" not in by_title                  # core set excluded
    assert by_title["Coin Collector"]["compatibility"] == "compatible"
    assert by_title["Coin Collector"]["patch_url"] == ""
    assert by_title["Ultimate"]["compatibility"] == "patch-required"
    assert by_title["Ultimate"]["patch_url"] == "https://ra/patch/x.zip"
    assert by_title["Ultimate"]["set_id"] == 9999


def test_refresh_game_sets_caches_per_game(fresh_engine, monkeypatch):
    from app.services import settings as app_settings
    from app.services import game_sets
    with Session(engine) as s:
        app_settings.set(s, "ra_api_key", "k")
        s.add(LibraryEntry(game_title="SM64", system="N64", file_name="x.z64",
                           file_path="/roms/N64/x.z64", ra_game_id=10003))
        s.commit()

    async def fake_get_game(self, game_id, include=""):
        assert "achievementSets" in include and "hashes" in include
        return _game_payload()
    monkeypatch.setattr(RAClientV2, "get_game", fake_get_game)

    res = asyncio.run(game_sets.refresh_game_sets())
    assert res["games"] == 1 and res["sets"] == 2
    with Session(engine) as s:
        rows = s.exec(select(RAGameSet).where(RAGameSet.game_id == 10003)).all()
        assert {r.title for r in rows} == {"Coin Collector", "Ultimate"}
        ultimate = next(r for r in rows if r.title == "Ultimate")
        assert ultimate.compatibility == "patch-required" and ultimate.patch_url.endswith("x.zip")

    # game_sets_for helper returns the cached rows for the detail panel.
    with Session(engine) as s:
        out = game_sets.game_sets_for(s, 10003)
    assert len(out) == 2


def test_refresh_game_sets_no_credentials(fresh_engine):
    from app.services import game_sets
    assert asyncio.run(game_sets.refresh_game_sets())["status"] == "no_credentials"
