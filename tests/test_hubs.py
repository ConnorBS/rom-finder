"""RA V2 hub import → Wanted: ref parsing, the games parser, the progress bucket, the
filterable PREVIEW, and the add-selected step with owned/wanted dedup. Network mocked."""
import base64
import json

import pytest
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import WantedGame, LibraryEntry, RAGameProgress
from app.services.ra_client_v2 import RAClientV2


@pytest.mark.parametrize("text,expected", [
    ("5417", 5417),
    ("/game/5417", 5417),
    ("https://retroachievements.org/game/5417-hacks-mario", 5417),
    ("/hub/99", 99),
    ("nonsense", None),
])
def test_parse_hub_ref(text, expected):
    from app.services.hubs import parse_hub_ref
    assert parse_hub_ref(text) == expected


@pytest.mark.parametrize("award,num,expected", [
    ("mastered", 50, "mastered"),
    ("beaten-hardcore", 3, "beaten"),
    ("beaten-softcore", 3, "beaten"),
    ("completed", 10, "beaten"),
    ("", 2, "some"),
    ("", 0, "none"),
])
def test_progress_bucket(award, num, expected):
    from app.services.hubs import progress_bucket
    assert progress_bucket(award, num) == expected


def test_games_from_payload_resolves_console_and_achievements():
    payload = {
        "data": [
            {"type": "games", "id": "10003",
             "attributes": {"title": "SM64", "achievementsPublished": 100, "pointsTotal": 745},
             "relationships": {"system": {"data": {"type": "systems", "id": "2"}}}},
            {"type": "games", "id": "724",
             "attributes": {"title": "Sonic"},  # no achievement count -> 0
             "relationships": {"system": {"data": {"type": "systems", "id": "1"}}}},
        ],
        "included": [
            {"type": "systems", "id": "2", "attributes": {"name": "Nintendo 64"}},
            {"type": "systems", "id": "1", "attributes": {"name": "Genesis"}},
        ],
    }
    games = RAClientV2.games_from_payload(payload)
    assert games[0]["game_id"] == 10003 and games[0]["console"] == "Nintendo 64"
    assert games[0]["achievements"] == 100 and games[0]["points"] == 745
    assert games[1]["console"] == "Genesis" and games[1]["achievements"] == 0


_PAYLOAD = {
    "data": [
        {"type": "games", "id": "10003",
         "attributes": {"title": "SM64", "achievementsPublished": 100},
         "relationships": {"system": {"data": {"type": "systems", "id": "2"}}}},
        {"type": "games", "id": "724",
         "attributes": {"title": "Sonic", "achievementsPublished": 50},
         "relationships": {"system": {"data": {"type": "systems", "id": "1"}}}},
        {"type": "games", "id": "999",
         "attributes": {"title": "New Game (USA)", "achievementsPublished": 0},
         "relationships": {"system": {"data": {"type": "systems", "id": "1"}}}},
    ],
    "included": [
        {"type": "systems", "id": "2", "attributes": {"name": "Nintendo 64"}},
        {"type": "systems", "id": "1", "attributes": {"name": "Genesis"}},
    ],
}


def _seed_owned_wanted():
    from app.services import settings as app_settings
    with Session(engine) as s:
        app_settings.set(s, "ra_api_key", "k")
        s.add(WantedGame(game_title="Already Wanted", system="N64", ra_game_id=10003))   # wanted
        s.add(LibraryEntry(game_title="Owned", system="Genesis", file_name="o.md",
                           file_path="/roms/o.md", ra_game_id=724))                       # owned
        s.commit()


def _mock_hub(monkeypatch):
    async def fake_hub_games(self, hub_id, page=1, size=100):
        return _PAYLOAD if page == 1 else {"data": []}
    monkeypatch.setattr(RAClientV2, "get_hub_games", fake_hub_games)


def test_import_hub_preview_annotates(client, monkeypatch):
    _seed_owned_wanted()
    _mock_hub(monkeypatch)
    r = client.post("/wanted/import-hub", data={"hub_ref": "https://retroachievements.org/hub/17723"})
    assert r.status_code == 200
    # All three games are previewed (nothing added yet)
    assert "SM64" in r.text and "Sonic" in r.text and "New Game" in r.text
    # Owned + already-wanted are flagged so they're pre-excluded
    assert "owned" in r.text and "wanted" in r.text
    # Each selectable game carries a checkbox + base64 token; nothing written to Wanted
    assert r.text.count('name="games"') == 3
    with Session(engine) as s:
        assert s.exec(select(WantedGame).where(WantedGame.ra_game_id == 999)).first() is None


def test_import_hub_add_selected_dedups(client, monkeypatch):
    _seed_owned_wanted()

    def tok(i, t, c):
        return base64.b64encode(json.dumps({"i": i, "t": t, "c": c}).encode()).decode()

    # User submits the new game + the owned + the already-wanted ones
    games = [tok(999, "New Game (USA)", "Genesis"),
             tok(724, "Sonic", "Genesis"),
             tok(10003, "SM64", "Nintendo 64")]
    r = client.post("/wanted/import-hub/add", data={"games": games, "hub_id": 17723})
    assert r.status_code == 200 and "Added 1 selected" in r.text
    with Session(engine) as s:
        new = s.exec(select(WantedGame).where(WantedGame.ra_game_id == 999)).first()
        assert new is not None and new.system == "Genesis"
        # 724 (owned) and 10003 (already wanted) were NOT added
        assert s.exec(select(WantedGame).where(WantedGame.ra_game_id == 724)).first() is None


def test_import_hub_add_none_selected(client):
    r = client.post("/wanted/import-hub/add", data={"hub_id": 0})
    assert r.status_code == 200 and "No games selected" in r.text


def test_import_hub_no_credentials(client):
    r = client.post("/wanted/import-hub", data={"hub_ref": "5417"})
    assert "credentials" in r.text.lower()
