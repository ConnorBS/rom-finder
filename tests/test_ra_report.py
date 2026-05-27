"""RA forum-markup report generation — correct shortcodes, tables, and the
preview/download routes."""

from datetime import datetime

from sqlmodel import Session

from app.db.database import engine
from app.db.models import RAProfile, RAAchievement, RAGameProgress
from app.services import ra_report


def _seed():
    with Session(engine) as s:
        s.add(RAProfile(id=1, username="connor", points=5000, rank=100,
                        total_achievements=2, total_games=1, total_masteries=1))
        s.add(RAAchievement(achievement_id=10, title="First Blood", points=10, true_ratio=50,
                            game_id=111, game_title="Contra", console_id=7, console_name="NES",
                            earned_at=datetime(2026, 1, 15), hardcore=True))
        s.add(RAAchievement(achievement_id=11, title="Sharpshooter", points=25, true_ratio=80,
                            game_id=111, game_title="Contra", console_id=7, console_name="NES",
                            earned_at=datetime(2026, 2, 20), hardcore=True))
        s.add(RAGameProgress(game_id=111, title="Contra", console_name="NES", max_possible=50,
                             num_awarded=2, pct_complete=4.0, owned=True))
        s.commit()


def test_time_period_recap(fresh_engine):
    _seed()
    with Session(engine) as s:
        md = ra_report.time_period_recap(s)
    assert md.startswith("## ")
    assert "2 achievements" in md and "35 points" in md       # 10 + 25
    assert "### Top games" in md and "Contra" in md
    assert "[ach=11]" in md                                    # rarest (TrueRatio 80) listed


def test_lifetime_showcase(fresh_engine):
    _seed()
    with Session(engine) as s:
        md = ra_report.lifetime_showcase(s)
    assert "[user=connor]" in md
    assert "5,000" in md and "### Top consoles" in md and "NES" in md


def test_per_game_writeup(fresh_engine):
    _seed()
    with Session(engine) as s:
        md = ra_report.per_game(s, 111)
    assert "[game=111]" in md and "2/50" in md
    assert "[ach=10]" in md and "First Blood" in md


def test_custom_view_honors_filters(fresh_engine):
    _seed()
    with Session(engine) as s:
        md = ra_report.custom_view(s, hardcore=True, console="NES")
    assert "NES" in md and "hardcore" in md and "2 achievements" in md


def test_report_routes(client):
    r = client.get("/dashboard/reports")
    assert r.status_code == 200
    r2 = client.get("/dashboard/reports/preview?report_type=lifetime")
    assert r2.status_code == 200 and "report-markup" in r2.text
    r3 = client.get("/dashboard/reports/download?report_type=lifetime")
    assert r3.status_code == 200 and r3.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r3.headers.get("content-disposition", "")
