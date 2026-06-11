"""Goals & events tracker.

Covers the two halves of the feature:
- LOCAL auto-completion (`services/goals.evaluate_goals`) against the RA mirror —
  hardcore only, so softcore awards never satisfy a goal, custom/unlinked goals
  never auto-flip, and a re-run is idempotent;
- the router CRUD + the `/api/status` and `/api/changes` `goals` signals.
"""
import pytest
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import Goal, GoalObjective, GoalStatus, RAGameProgress
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
