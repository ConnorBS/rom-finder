"""Goals & events tracker.

Covers the two halves of the feature:
- LOCAL auto-completion (`services/goals.evaluate_goals`) against the RA mirror —
  hardcore only, so softcore awards never satisfy a goal, custom/unlinked goals
  never auto-flip, and a re-run is idempotent;
- the router CRUD + the `/api/status` and `/api/changes` `goals` signals.
"""
import pytest
from sqlmodel import Session, select

from datetime import datetime

from app.db.database import engine
from app.db.models import Goal, GoalEvent, GoalObjective, GoalStatus, RAAchievement, RAGameProgress
from app.services.goals import evaluate_goals, award_satisfies


# --- evaluate_goals --------------------------------------------------------

def _seed_progress(s: Session, game_id: int, kind: str):
    s.add(RAGameProgress(game_id=game_id, title=f"Game {game_id}", max_possible=10,
                         num_awarded=10, pct_complete=100.0, highest_award_kind=kind))


def _seed_goal(s: Session, game_id, objective, **kw):
    g = Goal(game_title="G", system="PS1", ra_game_id=game_id, objective=objective, **kw)
    s.add(g)
    s.commit()
    s.refresh(g)
    return g.id


@pytest.mark.parametrize("kind,master_done,beaten_done", [
    ("mastered", True, True),
    ("beaten", False, True),
    ("beaten-softcore", False, False),  # softcore never counts (hardcore-required)
    ("completed", False, False),        # softcore 100% is not a hardcore beating
    ("", False, False),
])
def test_award_satisfies_hardcore_only(kind, master_done, beaten_done):
    assert award_satisfies(GoalObjective.master, kind) is master_done
    assert award_satisfies(GoalObjective.beaten, kind) is beaten_done
    assert award_satisfies(GoalObjective.custom, kind) is False


def test_evaluate_flips_satisfied_and_stamps(fresh_engine):
    with Session(engine) as s:
        _seed_progress(s, 100, "mastered")
        _seed_progress(s, 200, "beaten")
        s.commit()
        gid_master = _seed_goal(s, 100, GoalObjective.master)
        gid_beaten = _seed_goal(s, 200, GoalObjective.beaten)

    with Session(engine) as s:
        out = evaluate_goals(s)
    assert out["completed"] == 2

    with Session(engine) as s:
        for gid in (gid_master, gid_beaten):
            g = s.get(Goal, gid)
            assert g.status == GoalStatus.completed
            assert g.auto is True
            assert g.completed_at is not None


def test_evaluate_does_not_flip_unsatisfied_custom_or_unlinked(fresh_engine):
    with Session(engine) as s:
        _seed_progress(s, 100, "beaten-softcore")  # softcore — must not satisfy master/beat
        s.commit()
        gid_master = _seed_goal(s, 100, GoalObjective.master)
        gid_beaten = _seed_goal(s, 100, GoalObjective.beaten)
        # custom goal pointing at a mastered game still must not auto-flip
        _seed_progress(s, 300, "mastered")
        s.commit()
        gid_custom = _seed_goal(s, 300, GoalObjective.custom, custom_text="finish level 5")
        # RA-linked beat goal with no mirror row at all
        gid_nomirror = _seed_goal(s, 999, GoalObjective.beaten)

    with Session(engine) as s:
        assert evaluate_goals(s)["completed"] == 0
        for gid in (gid_master, gid_beaten, gid_custom, gid_nomirror):
            assert s.get(Goal, gid).status == GoalStatus.active


def test_evaluate_is_idempotent(fresh_engine):
    with Session(engine) as s:
        _seed_progress(s, 100, "mastered")
        s.commit()
        _seed_goal(s, 100, GoalObjective.master)
    with Session(engine) as s:
        assert evaluate_goals(s)["completed"] == 1
    with Session(engine) as s:
        assert evaluate_goals(s)["completed"] == 0  # already done, nothing new to flip


def test_evaluate_achievement_goal_hardcore_only(fresh_engine):
    with Session(engine) as s:
        # achievement 555 earned in SOFTCORE only — must NOT satisfy the goal
        s.add(RAAchievement(achievement_id=555, game_id=42, earned_at=datetime.utcnow(), hardcore=False))
        s.commit()
        g = Goal(game_title="Game", ra_game_id=42, achievement_id=555,
                 objective=GoalObjective.achievement, custom_text="Beat the boss")
        s.add(g)
        s.commit()
        s.refresh(g)
        gid = g.id

    with Session(engine) as s:
        assert evaluate_goals(s)["completed"] == 0
        assert s.get(Goal, gid).status == GoalStatus.active

    # Now earn it in hardcore → the goal completes.
    with Session(engine) as s:
        s.add(RAAchievement(achievement_id=555, game_id=42, earned_at=datetime.utcnow(), hardcore=True))
        s.commit()
    with Session(engine) as s:
        assert evaluate_goals(s)["completed"] == 1
        g = s.get(Goal, gid)
        assert g.status == GoalStatus.completed and g.auto is True


# --- extension API ---------------------------------------------------------

def test_api_goal_achievement_add_dedup_and_status(client):
    payload = {
        "ra_game_id": 42, "game_title": "Some Game", "system": "", "system_id": 12,
        "objective": "achievement", "achievement_id": 555,
        "achievement_title": "Beat the boss", "event_name": "Collectathon",
        "deadline": "2026-07-18",
    }
    r = client.post("/api/goal", json=payload)
    assert r.status_code == 200 and r.json()["status"] == "added"

    # Re-post the same achievement → exists, not a duplicate row.
    r = client.post("/api/goal", json=payload)
    assert r.json()["status"] == "exists"

    with Session(engine) as s:
        goals = s.exec(select(Goal)).all()
        assert len(goals) == 1
        g = goals[0]
        assert g.objective == "achievement" and g.achievement_id == 555
        assert g.custom_text == "Beat the boss"
        assert g.system == "PlayStation"  # resolved from system_id=12 via canonical_system
        assert g.deadline.month == 7 and g.deadline.day == 18

    st = client.get("/api/goal-status?ra_game_id=42&achievement_id=555").json()
    assert st["goal"] is True and st["completed"] is False and st["objective"] == "achievement"

    # A game-level query (no achievement_id) must NOT match the achievement goal.
    st2 = client.get("/api/goal-status?ra_game_id=42").json()
    assert st2["goal"] is False


def test_api_goal_requires_achievement_id(client):
    r = client.post("/api/goal", json={
        "ra_game_id": 1, "game_title": "G", "objective": "achievement",
    })
    assert r.json()["status"] == "error"


def test_api_goal_game_level(client):
    r = client.post("/api/goal", json={
        "ra_game_id": 99, "game_title": "Ape Escape", "system_id": 12,
        "objective": "master", "deadline": "2026-07-31",
    })
    assert r.json()["status"] == "added"
    with Session(engine) as s:
        g = s.exec(select(Goal).where(Goal.ra_game_id == 99)).first()
        assert g.objective == "master" and g.achievement_id is None


def test_api_events_distinct(client):
    client.post("/api/goal", json={"ra_game_id": 1, "game_title": "A", "objective": "master",
                                    "event_name": "Collectathon"})
    client.post("/api/goal", json={"ra_game_id": 2, "game_title": "B", "objective": "beaten",
                                    "event_name": "Collectathon"})  # same event, different game
    client.post("/api/goal", json={"ra_game_id": 3, "game_title": "C", "objective": "master",
                                    "event_name": "Wii rollout"})
    client.post("/api/goal", json={"ra_game_id": 4, "game_title": "D", "objective": "master"})  # no event
    events = client.get("/api/events").json()["events"]
    assert events == ["Collectathon", "Wii rollout"]  # distinct, sorted, no blank


def test_add_achievement_goal_from_page(client):
    # The /goals page flow: achievement_id present → objective forced to achievement,
    # achievement_title stored as the card label (desc/badge fill in via background enrich).
    r = client.post("/goals/add", data={
        "ra_game_id": "42", "game_title": "Some Game", "system": "PlayStation",
        "objective": "beaten",  # ignored because an achievement_id is supplied
        "achievement_id": "555", "achievement_title": "Beat the boss",
        "event_name": "Collectathon", "deadline": "2026-07-18",
    })
    assert r.status_code == 200
    with Session(engine) as s:
        g = s.exec(select(Goal).where(Goal.ra_game_id == 42)).first()
        assert g.objective == "achievement"
        assert g.achievement_id == 555
        assert g.custom_text == "Beat the boss"
        assert g.event_name == "Collectathon"


def test_achievements_endpoint_without_creds(client):
    # No RA creds seeded in the test app → endpoint returns a friendly notice, not a 500.
    r = client.get("/ra/games/42/achievements")
    assert r.status_code == 200
    assert "credentials" in r.text.lower()


# --- router CRUD -----------------------------------------------------------

def test_add_ra_and_custom_goal(client):
    r = client.post("/goals/add", data={
        "ra_game_id": "10003", "game_title": "Ape Escape", "system": "PlayStation",
        "objective": "master", "event_name": "Collectathon", "deadline": "2026-07-31",
    })
    assert r.status_code == 200

    r = client.post("/goals/add-custom", data={
        "game_title": "Glitch", "system": "GameCube",
        "custom_text": "finish level 5", "event_name": "", "deadline": "",
    })
    assert r.status_code == 200

    with Session(engine) as s:
        goals = s.exec(select(Goal)).all()
        assert len(goals) == 2
        ra = next(g for g in goals if g.ra_game_id == 10003)
        assert ra.objective == "master"
        assert ra.event_name == "Collectathon"
        assert ra.deadline.year == 2026 and ra.deadline.month == 7 and ra.deadline.day == 31
        custom = next(g for g in goals if g.objective == "custom")
        assert custom.custom_text == "finish level 5"
        assert custom.deadline is None  # empty string parses to None


def test_complete_reopen_edit_delete(client):
    client.post("/goals/add-custom", data={
        "game_title": "Glitch", "system": "GameCube", "custom_text": "finish level 5",
    })
    with Session(engine) as s:
        gid = s.exec(select(Goal)).first().id

    assert client.post(f"/goals/{gid}/complete").status_code == 200
    with Session(engine) as s:
        g = s.get(Goal, gid)
        assert g.status == GoalStatus.completed and g.auto is False

    assert client.post(f"/goals/{gid}/reopen").status_code == 200
    with Session(engine) as s:
        assert s.get(Goal, gid).status == GoalStatus.active

    client.post(f"/goals/{gid}/edit", data={"event_name": "Backlog", "deadline": "2026-08-01"})
    with Session(engine) as s:
        g = s.get(Goal, gid)
        assert g.event_name == "Backlog" and g.deadline.month == 8

    r = client.delete(f"/goals/{gid}")
    assert r.status_code == 200 and r.text == ""
    with Session(engine) as s:
        assert s.get(Goal, gid) is None


# --- status + changes signals ---------------------------------------------

def test_status_goals_section(client):
    client.post("/goals/add", data={
        "ra_game_id": "1", "game_title": "A", "system": "NES",
        "objective": "beaten", "deadline": "2000-01-01",  # in the past → overdue
    })
    client.post("/goals/add-custom", data={"game_title": "B", "custom_text": "x"})

    g = client.get("/api/status").json()["goals"]
    assert "error" not in g
    assert g["total"] == 2
    assert g["active"] == 2
    assert g["custom"] == 1
    assert g["overdue"] == 1


def test_changes_goals_token_moves(client):
    data = client.get("/api/changes").json()
    assert "goals" in data and not str(data["goals"]).startswith("err:")
    base = data["goals"]

    client.post("/goals/add-custom", data={"game_title": "B", "custom_text": "x"})
    after_add = client.get("/api/changes").json()["goals"]
    assert after_add != base

    with Session(engine) as s:
        gid = s.exec(select(Goal)).first().id
    client.post(f"/goals/{gid}/complete")
    after_complete = client.get("/api/changes").json()["goals"]
    assert after_complete != after_add


# --- event import / nightly sync / custom events ---------------------------

def _ach(aid, title, desc="desc", points=5, badge="12345"):
    return {"ID": aid, "Title": title, "Description": desc, "Points": points,
            "BadgeName": badge, "DisplayOrder": aid}


def _extended(title="Test Event", console="Events", achs=None):
    achs = achs or []
    return {"ID": 196, "Title": title, "ConsoleName": console,
            "Achievements": {str(a["ID"]): a for a in achs}}


def _seed_creds():
    from app.services import settings as app_settings
    with Session(engine) as s:
        app_settings.set(s, "ra_username", "u")
        app_settings.set(s, "ra_api_key", "k")


def test_build_event_goals_skips_placeholders_and_dedup(fresh_engine):
    from app.services.events import build_event_goals
    achs = [
        {"id": 1, "title": "A", "description": "da", "points": 5, "badge_url": "u1"},
        {"id": 2, "title": "B", "description": "db", "points": 10, "badge_url": ""},  # placeholder
        {"id": 3, "title": "C", "description": "dc", "points": 7, "badge_url": "u3"},
    ]
    with Session(engine) as s:
        s.add(Goal(game_title="E", ra_game_id=196, achievement_id=1, objective=GoalObjective.achievement))
        s.commit()
        stats = build_event_goals(s, ra_game_id=196, event_name="E", game_title="E", system="Events",
                                  achievements=achs, include_completed=True, deadline=None)
        s.commit()
    assert stats == {"created": 1, "skipped_existing": 1, "skipped_placeholder": 1, "skipped_done": 0}
    with Session(engine) as s:
        g = s.exec(select(Goal).where(Goal.achievement_id == 3)).first()
        assert g.points == 7 and g.cover_path == "u3" and g.objective == "achievement"


def test_build_event_goals_excludes_already_earned_when_unchecked(fresh_engine):
    from app.services.events import build_event_goals
    achs = [{"id": 10, "title": "X", "description": "", "points": 1, "badge_url": "u"}]
    with Session(engine) as s:
        s.add(RAAchievement(achievement_id=10, game_id=196, earned_at=datetime.utcnow(), hardcore=True))
        s.commit()
        stats = build_event_goals(s, ra_game_id=196, event_name="E", game_title="E", system="",
                                  achievements=achs, include_completed=False, deadline=None)
        s.commit()
    assert stats["created"] == 0 and stats["skipped_done"] == 1


def test_import_event_endpoint(client, monkeypatch):
    from app.services.ra_client import RAClient
    payload = _extended(achs=[_ach(1, "A"), _ach(2, "B", badge="00000"), _ach(3, "C")])

    async def fake(self, gid):
        return payload
    monkeypatch.setattr(RAClient, "get_game_extended", fake)
    _seed_creds()

    r = client.post("/goals/import-event",
                    data={"event_ref": "https://retroachievements.org/event/196-x", "include_completed": "true"})
    assert r.status_code == 200 and "Imported 2" in r.text   # ach 2 is a placeholder tile, skipped
    with Session(engine) as s:
        goals = s.exec(select(Goal).where(Goal.ra_game_id == 196)).all()
        assert len(goals) == 2
        assert all(g.objective == "achievement" for g in goals)
        ev = s.exec(select(GoalEvent).where(GoalEvent.ra_game_id == 196)).first()
        assert ev.auto_sync is True and ev.name == "Test Event"


def test_custom_event_with_link(client):
    r = client.post("/goals/event", data={"name": "Summer Backlog", "url": "https://docs.google.com/sheet"})
    assert r.status_code == 200
    with Session(engine) as s:
        ev = s.exec(select(GoalEvent).where(GoalEvent.name == "Summer Backlog")).first()
        assert ev.url == "https://docs.google.com/sheet" and ev.auto_sync is False and ev.ra_game_id is None


def test_event_nightly_sync_adds_new_achievements(client, monkeypatch):
    from app.services.ra_client import RAClient
    _seed_creds()
    state = {"achs": [_ach(1, "A")]}

    async def fake(self, gid):
        return _extended(achs=state["achs"])
    monkeypatch.setattr(RAClient, "get_game_extended", fake)

    client.post("/goals/import-event", data={"event_ref": "196", "include_completed": "true"})
    with Session(engine) as s:
        assert len(s.exec(select(Goal).where(Goal.ra_game_id == 196)).all()) == 1

    # The event grows (AotW/random rolls) — nightly sync picks up the new achievement.
    state["achs"] = [_ach(1, "A"), _ach(2, "B")]
    r = client.post("/scheduler/run/eventsync")
    assert r.status_code == 200
    with Session(engine) as s:
        assert len(s.exec(select(Goal).where(Goal.ra_game_id == 196)).all()) == 2
