"""Dashboard pages render from the local mirror (no RA calls) and the Timeline
filters work. Refresh kicks a background task only when credentials are set."""

from datetime import datetime

from sqlmodel import Session

from app.db.database import engine
from app.db.models import RAProfile, RAAchievement, RAGameProgress
from app.services import settings as app_settings


def _seed_profile_and_unlocks():
    with Session(engine) as s:
        s.add(RAProfile(id=1, username="u", points=1234, points_softcore=10, rank=42,
                        total_achievements=2, total_games=1, total_masteries=1,
                        last_synced_at=datetime(2026, 5, 1, 12, 0)))
        s.add(RAAchievement(achievement_id=1, title="First Blood", points=10, game_id=111,
                            game_title="Contra", console_id=7, console_name="NES",
                            earned_at=datetime(2026, 1, 15, 9, 0), hardcore=True))
        s.add(RAAchievement(achievement_id=2, title="Sharpshooter", points=25, game_id=222,
                            game_title="Mega Man", console_id=7, console_name="NES",
                            earned_at=datetime(2026, 3, 20, 18, 0), hardcore=False))
        s.add(RAGameProgress(game_id=111, title="Contra", console_name="NES", max_possible=50,
                             num_awarded=50, pct_complete=100.0, highest_award_kind="mastered", owned=True))
        s.commit()


def test_overview_empty_state(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "No RetroAchievements data synced yet" in r.text


def test_overview_renders_with_data(client):
    _seed_profile_and_unlocks()
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "1,234" in r.text                         # points tile
    assert "of 1 owned games mastered/completed" in r.text
    assert "First Blood" in r.text                   # recent unlock
    assert "ptsChart" in r.text                      # chart wired


def test_timeline_search_filter(client):
    _seed_profile_and_unlocks()
    r = client.get("/dashboard/timeline?q=Sharpshooter")
    assert r.status_code == 200
    assert "Sharpshooter" in r.text
    assert "First Blood" not in r.text               # filtered out by name search


def test_timeline_console_and_mode_filter(client):
    _seed_profile_and_unlocks()
    r = client.get("/dashboard/timeline?hardcore=hardcore")
    assert "First Blood" in r.text and "Sharpshooter" not in r.text


def test_games_page_owned_highlight_and_filter(client):
    _seed_profile_and_unlocks()
    r = client.get("/dashboard/games")
    assert r.status_code == 200
    assert "Contra" in r.text and ">owned<" in r.text     # owned badge rendered
    # owned-only filter keeps Contra (it's owned)
    r2 = client.get("/dashboard/games?owned=1")
    assert "Contra" in r2.text


def test_insights_page_renders(client):
    _seed_profile_and_unlocks()
    r = client.get("/dashboard/insights")
    assert r.status_code == 200
    assert "consoleChart" in r.text                       # chart wired
    assert "NES" in r.text                                # by-console table
    assert "Longest daily streak" in r.text


def test_insights_empty_state(client):
    r = client.get("/dashboard/insights")
    assert r.status_code == 200
    assert "No achievement data yet" in r.text


def test_api_status_dashboard_section(client):
    _seed_profile_and_unlocks()
    r = client.get("/api/status")
    assert r.status_code == 200
    d = r.json().get("dashboard", {})
    assert d.get("achievements") == 2 and d.get("games") == 1


def test_refresh_requires_credentials(client):
    r = client.post("/dashboard/refresh")
    assert r.status_code == 200
    assert "credentials in Settings" in r.text


def test_refresh_kicks_task_when_configured(client, monkeypatch):
    calls = {"n": 0}

    async def fake_refresh():
        calls["n"] += 1
        return {"status": "ok"}

    monkeypatch.setattr("app.services.ra_dashboard.refresh", fake_refresh)
    with Session(engine) as s:
        app_settings.set(s, "ra_username", "u")
        app_settings.set(s, "ra_api_key", "k")
        s.commit()
    r = client.post("/dashboard/refresh")
    assert r.status_code == 200
    assert "Syncing from RetroAchievements" in r.text
    assert calls["n"] == 1                            # background task ran
