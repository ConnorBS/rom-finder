"""Extension-facing /api endpoints: the game-status pre-check and the cover-task
argument fix (api.py used to pass the RA username where the game title belongs)."""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import settings as app_settings


def test_game_status_unknown(client):
    r = client.get("/api/game-status?ra_game_id=999999")
    assert r.status_code == 200
    assert r.json() == {
        "ra_game_id": 999999, "wanted": False, "wanted_status": None, "owned": False,
    }


def test_game_status_reports_wanted(client):
    client.post("/api/wanted", json={
        "ra_game_id": 12345, "game_title": "Some Game", "system": "GameCube", "system_id": 16,
    })
    data = client.get("/api/game-status?ra_game_id=12345").json()
    assert data["wanted"] is True
    assert data["wanted_status"] == "hunting"
    assert data["owned"] is False


def test_game_status_reports_owned(client):
    with Session(engine) as s:
        s.add(LibraryEntry(
            game_title="Owned Game", system="GameCube", file_name="g.iso",
            file_path="/roms/g.iso", ra_game_id=777, ra_matched=True,
        ))
        s.commit()
    data = client.get("/api/game-status?ra_game_id=777").json()
    assert data["owned"] is True
    assert data["wanted"] is False


def test_add_wanted_passes_title_not_credentials_to_cover(client, monkeypatch):
    # Regression: api.py used to call _fetch_cover(id, ra_game_id, username, api_key),
    # so a title-based cover source (SteamGridDB) searched the RA *username* and
    # fetched one identical image for every extension-added game.
    import app.routers.wanted as wanted_mod

    calls = []

    async def _fake_fetch_cover(wanted_id, ra_game_id, game_title, system, batch_id=""):
        calls.append({"game_title": game_title, "system": system})

    monkeypatch.setattr(wanted_mod, "_fetch_cover", _fake_fetch_cover)

    with Session(engine) as s:
        app_settings.set(s, "ra_username", "MyRAUser")
        app_settings.set(s, "ra_api_key", "SECRETKEY")

    r = client.post("/api/wanted", json={
        "ra_game_id": 25426, "game_title": "Billy Hatcher and the Giant Egg",
        "system": "", "system_id": 16,
    })
    assert r.status_code == 200
    assert calls, "cover-fetch task was not scheduled"
    assert calls[0]["game_title"] == "Billy Hatcher and the Giant Egg"
    assert calls[0]["system"] == "GameCube"  # resolved server-side from system_id=16
    assert "MyRAUser" not in calls[0].values()
    assert "SECRETKEY" not in calls[0].values()
