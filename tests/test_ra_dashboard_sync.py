"""RA dashboard sync: builds the local mirror, dedupes window overlaps, sets the
owned-library cross-ref, and (critically) REPLACES the mirror each run so
retroactive RA changes (repointed/removed achievements) reconcile rather than drift."""

import asyncio
from datetime import datetime

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import RAAchievement, RAGameProgress, RAProfile, LibraryEntry
from app.services import settings as app_settings
from app.services import ra_dashboard

_ACH = {
    "AchievementID": 1, "Title": "First Blood", "Description": "d", "Points": 10,
    "TrueRatio": 25, "Type": "progression", "GameID": 111, "GameTitle": "Contra",
    "ConsoleID": 7, "ConsoleName": "NES", "BadgeURL": "b",
    "Date": "2025-02-01 12:00:00", "HardcoreMode": 1,
}
_GAME = {
    "GameID": 111, "Title": "Contra", "ConsoleID": 7, "ConsoleName": "NES",
    "ImageIcon": "i", "MaxPossible": 50, "NumAwarded": 50, "NumAwardedHardcore": 50,
    # API_GetUserCompletionProgress sends ISO-8601 with a 'T' + timezone offset (NOT the
    # space-separated form the other endpoints use) — _parse_dt must handle it or every
    # award date becomes None and master/beaten goals auto-complete with the sync date.
    "HighestAwardKind": "mastered", "HighestAwardDate": "2025-02-02T00:00:00+00:00",
    "MostRecentAwardedDate": "2025-02-01T12:00:00+00:00",
}


class FakeRA:
    profile = {"User": "u", "TotalPoints": 1000, "TotalSoftcorePoints": 50,
               "Rank": 7, "MemberSince": "2025-01-01 00:00:00"}
    achievements: list = [dict(_ACH)]
    completion = {"Count": 1, "Total": 1, "Results": [dict(_GAME)]}
    awards = {"MasteryAwardsCount": 2}

    def __init__(self, u, k):
        pass

    @staticmethod
    def badge_url(name):   # mirrors RAClient.badge_url (step 2b builds BadgeURL with it)
        return f"https://media.retroachievements.org/Badge/{name}.png" if name else ""

    async def get_user_profile(self, user=None):
        return FakeRA.profile

    async def get_achievements_earned_between(self, f, t, user=None):
        return [dict(a) for a in FakeRA.achievements]   # same set every window → tests dedup

    async def get_user_completion_progress(self, count=500, offset=0, user=None):
        if offset == 0:
            return FakeRA.completion
        return {"Count": 0, "Total": FakeRA.completion["Total"], "Results": []}

    async def get_user_awards(self, user=None):
        return FakeRA.awards


def _set_creds():
    with Session(engine) as s:
        app_settings.set(s, "ra_username", "u")
        app_settings.set(s, "ra_api_key", "k")
        s.commit()


def test_refresh_populates_dedupes_and_links_owned(fresh_engine, monkeypatch):
    FakeRA.achievements = [dict(_ACH)]
    _set_creds()
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Contra", system="NES", file_name="c.nes",
                           file_path="/c.nes", ra_game_id=111))
        s.commit()
    monkeypatch.setattr(ra_dashboard, "RAClient", FakeRA)

    res = asyncio.run(ra_dashboard.refresh())
    assert res["status"] == "ok"

    with Session(engine) as s:
        achs = s.exec(select(RAAchievement)).all()
        games = s.exec(select(RAGameProgress)).all()
        prof = s.get(RAProfile, 1)
    assert len(achs) == 1                              # de-duped across ~9 windows
    assert achs[0].title == "First Blood" and achs[0].points == 10 and achs[0].hardcore
    assert len(games) == 1 and games[0].owned and games[0].pct_complete == 100.0
    assert games[0].highest_award_kind == "mastered"
    # ISO-8601 award dates must parse to naive-UTC datetimes (not None) — this is what
    # lets a master/beaten goal complete with the real mastery date, not the sync date.
    assert games[0].highest_award_date == datetime(2025, 2, 2, 0, 0, 0)
    assert games[0].most_recent_date == datetime(2025, 2, 1, 12, 0, 0)
    assert prof.points == 1000 and prof.total_masteries == 2 and prof.last_synced_at is not None
    with Session(engine) as s:
        assert app_settings.get(s, "ra_dashboard_last_sync") != ""


def test_refresh_reconciles_retroactive_changes(fresh_engine, monkeypatch):
    _set_creds()
    monkeypatch.setattr(ra_dashboard, "RAClient", FakeRA)

    FakeRA.achievements = [dict(_ACH, Points=10)]
    asyncio.run(ra_dashboard.refresh())
    with Session(engine) as s:
        assert s.exec(select(RAAchievement)).one().points == 10

    # RA repoints the achievement → re-sync REPLACES (not appends).
    FakeRA.achievements = [dict(_ACH, Points=5)]
    asyncio.run(ra_dashboard.refresh())
    with Session(engine) as s:
        rows = s.exec(select(RAAchievement)).all()
    assert len(rows) == 1 and rows[0].points == 5

    # RA removes the achievement entirely → mirror empties.
    FakeRA.achievements = []
    asyncio.run(ra_dashboard.refresh())
    with Session(engine) as s:
        assert s.exec(select(RAAchievement)).all() == []


def test_refresh_no_credentials(fresh_engine, monkeypatch):
    monkeypatch.setattr(ra_dashboard, "RAClient", FakeRA)
    res = asyncio.run(ra_dashboard.refresh())
    assert res["status"] == "no_credentials"


def test_refresh_backfills_achievement_goal_source_game(fresh_engine, monkeypatch):
    """The windowed earned-between pull under-reports individual unlocks (it dropped
    a Jun-24 hardcore unlock on game 14811 that had an achievement goal). The refresh
    backfills the source game of every achievement goal per-game, so the unlock lands
    in the mirror and the goal auto-completes — the 'Welcome To The Next Level' case."""
    from app.db.models import Goal, GoalObjective, GoalStatus, RAAchievement
    _set_creds()
    with Session(engine) as s:
        s.add(Goal(game_title="Sonic & Sega All-Stars Racing", system="Nintendo DS",
                   ra_game_id=14811, objective=GoalObjective.achievement,
                   achievement_id=145704, status=GoalStatus.active))
        s.commit()

    class FakeRAGoalGame(FakeRA):
        # Windowed pull + completion return nothing for this game (the under-report);
        # only the per-game backfill knows the unlock.
        async def get_achievements_earned_between(self, f, t, user=None):
            return []

        async def get_user_completion_progress(self, count=500, offset=0, user=None):
            return {"Count": 0, "Total": 0, "Results": []}

        async def get_game_user_progress(self, game_id, user=None):
            assert game_id == 14811
            return {
                "Title": "Sonic & Sega All-Stars Racing", "ConsoleID": 24,
                "ConsoleName": "Nintendo DS",
                "Achievements": {
                    "145704": {
                        "ID": 145704, "Title": "Welcome To The Next Level!",
                        "Description": "Win every race within a Grand Prix cup.",
                        "Points": 5, "BadgeName": "12345",
                        "DateEarnedHardcore": "2026-06-24 12:00:00",
                    },
                },
            }

    monkeypatch.setattr(ra_dashboard, "RAClient", FakeRAGoalGame)
    asyncio.run(ra_dashboard.refresh())

    with Session(engine) as s:
        ach = s.exec(select(RAAchievement).where(RAAchievement.achievement_id == 145704)).all()
        assert len(ach) == 1 and ach[0].hardcore, "backfill must add the under-reported unlock"
        goal = s.exec(select(Goal).where(Goal.achievement_id == 145704)).one()
        assert goal.status == GoalStatus.completed, "goal auto-completes off the backfilled unlock"


def test_refresh_stamps_ok_status(fresh_engine, monkeypatch):
    FakeRA.achievements = [dict(_ACH)]
    _set_creds()
    monkeypatch.setattr(ra_dashboard, "RAClient", FakeRA)
    asyncio.run(ra_dashboard.refresh())
    with Session(engine) as s:
        assert app_settings.get(s, "ra_dashboard_last_status") == "ok"
        assert app_settings.get(s, "ra_dashboard_last_error") == ""


def test_refresh_rate_limited_preserves_mirror_and_flags_status(fresh_engine, monkeypatch):
    """A 429 mid-pull must NOT wipe the existing mirror (it never reaches the
    replace step) and must surface a 'rate_limited' status — otherwise the page
    shows stale data with no explanation ('Refresh did nothing')."""
    from app.services.sources.errors import SourceRateLimitError

    FakeRA.achievements = [dict(_ACH)]
    _set_creds()
    monkeypatch.setattr(ra_dashboard, "RAClient", FakeRA)
    asyncio.run(ra_dashboard.refresh())                 # seed a good mirror
    with Session(engine) as s:
        assert len(s.exec(select(RAAchievement)).all()) == 1

    class FakeRARateLimited(FakeRA):
        async def get_achievements_earned_between(self, f, t, user=None):
            raise SourceRateLimitError("throttled", retry_after=1.0)

    monkeypatch.setattr(ra_dashboard, "RAClient", FakeRARateLimited)
    res = asyncio.run(ra_dashboard.refresh())

    assert res["status"] == "rate_limited"
    with Session(engine) as s:
        # mirror preserved (not wiped by a failed run), status surfaced
        assert len(s.exec(select(RAAchievement)).all()) == 1
        assert app_settings.get(s, "ra_dashboard_last_status") == "rate_limited"
        assert app_settings.get(s, "ra_dashboard_last_error") != ""
