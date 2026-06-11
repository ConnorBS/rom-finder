"""RA V2 hub import → Wanted: ref parsing, the games parser, and bulk-add with
owned/wanted dedup. Network mocked."""
import pytest
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import WantedGame, LibraryEntry
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


def test_games_from_payload_resolves_console():
    payload = {
        "data": [
            {"type": "games", "id": "10003", "attributes": {"title": "SM64"},
             "relationships": {"system": {"data": {"type": "systems", "id": "2"}}}},
            {"type": "games", "id": "724", "attributes": {"title": "Sonic"},
             "relationships": {"system": {"data": {"type": "systems", "id": "1"}}}},
        ],
        "included": [
            {"type": "systems", "id": "2", "attributes": {"name": "Nintendo 64"}},
            {"type": "systems", "id": "1", "attributes": {"name": "Genesis"}},
        ],
    }
    games = RAClientV2.games_from_payload(payload)
    assert games[0] == {"game_id": 10003, "title": "SM64", "console": "Nintendo 64"}
    assert games[1]["console"] == "Genesis"


def test_import_hub_adds_and_dedups(client, monkeypatch):
    from app.services import settings as app_settings
    with Session(engine) as s:
        app_settings.set(s, "ra_api_key", "k")
        s.add(WantedGame(game_title="Already Wanted", system="N64", ra_game_id=10003))   # skip: wanted
        s.add(LibraryEntry(game_title="Owned", system="Genesis", file_name="o.md",
                           file_path="/roms/o.md", ra_game_id=724))                       # skip: owned
        s.commit()

    payload_page = {
        "data": [
            {"type": "games", "id": "10003", "attributes": {"title": "SM64"},
             "relationships": {"system": {"data": {"type": "systems", "id": "2"}}}},
            {"type": "games", "id": "724", "attributes": {"title": "Sonic"},
             "relationships": {"system": {"data": {"type": "systems", "id": "1"}}}},
            {"type": "games", "id": "999", "attributes": {"title": "New Game (USA)"},
             "relationships": {"system": {"data": {"type": "systems", "id": "1"}}}},
        ],
        "included": [
            {"type": "systems", "id": "2", "attributes": {"name": "Nintendo 64"}},
            {"type": "systems", "id": "1", "attributes": {"name": "Genesis"}},
        ],
    }

    async def fake_hub_games(self, hub_id, page=1, size=100):
        return payload_page if page == 1 else {"data": []}
    monkeypatch.setattr(RAClientV2, "get_hub_games", fake_hub_games)

    r = client.post("/wanted/import-hub", data={"hub_ref": "https://retroachievements.org/game/5417-hacks"})
    assert r.status_code == 200 and "Added 1 of 3" in r.text   # only id 999 is new
    with Session(engine) as s:
        new = s.exec(select(WantedGame).where(WantedGame.ra_game_id == 999)).first()
        assert new is not None and new.system == "Genesis"


def test_import_hub_no_credentials(client):
    r = client.post("/wanted/import-hub", data={"hub_ref": "5417"})
    assert "credentials" in r.text.lower()
