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
from app.db.models import Goal, GoalEvent, GoalCategory, GoalObjective, GoalStatus, RAAchievement, RAGameProgress
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


def test_completed_at_is_real_unlock_date_and_self_heals(fresh_engine):
    """completed_at must be the RA unlock/beat date, not the evaluator's run time — and an
    already-completed auto goal carrying the wrong (sync) date is self-healed."""
    unlock = datetime(2026, 3, 14, 9, 30, 0)
    with Session(engine) as s:
        s.add(RAAchievement(achievement_id=553117, game_id=35105, earned_at=unlock, hardcore=True))
        s.add(RAGameProgress(game_id=100, title="G", max_possible=10, num_awarded=10,
                             pct_complete=100.0, highest_award_kind="mastered",
                             highest_award_date=unlock))
        s.commit()
        ach = Goal(game_title="Mr Bean", ra_game_id=35105, achievement_id=553117,
                   objective=GoalObjective.achievement, custom_text="Golden Retriever")
        master = Goal(game_title="G", ra_game_id=100, objective=GoalObjective.master)
        s.add(ach); s.add(master); s.commit()
        s.refresh(ach); s.refresh(master)
        agid, mgid = ach.id, master.id

    with Session(engine) as s:
        evaluate_goals(s)
        assert s.get(Goal, agid).completed_at == unlock   # the achievement's hardcore date
        assert s.get(Goal, mgid).completed_at == unlock   # the game's mastery date

    # Simulate the pre-fix wrong date, then confirm a re-run corrects it.
    with Session(engine) as s:
        g = s.get(Goal, agid); g.completed_at = datetime(2026, 6, 11, 20, 0, 0); s.add(g); s.commit()
    with Session(engine) as s:
        assert evaluate_goals(s)["corrected"] >= 1
        assert s.get(Goal, agid).completed_at == unlock


def test_resolve_event_source_game_by_name_and_desc(fresh_engine):
    from app.services.goals import resolve_event_source_games
    with Session(engine) as s:
        # SOURCE achievement on a real console (Mr. Bean / Wii)
        s.add(RAAchievement(achievement_id=553117, game_id=35105, game_title="Mr. Bean's Wacky World",
                            console_id=19, console_name="Wii", title="Golden Retriever",
                            description="Collect all golden teddies", earned_at=datetime.utcnow(), hardcore=True))
        # the EVENT-console clone of the same achievement — must be ignored as a source
        s.add(RAAchievement(achievement_id=609327, game_id=38548, game_title="Collect-a-thon MaRAthon",
                            console_id=101, console_name="Events", title="Golden Retriever",
                            description="Collect all golden teddies", earned_at=datetime.utcnow(), hardcore=True))
        s.commit()
        g = Goal(game_title="Collect-a-thon MaRAthon", system="Events", ra_game_id=38548,
                 achievement_id=609327, objective=GoalObjective.achievement,
                 custom_text="Golden Retriever", achievement_desc="Collect all golden teddies",
                 event_name="Collect-a-thon MaRAthon")
        s.add(g); s.commit(); s.refresh(g)
        gid = g.id
    with Session(engine) as s:
        assert resolve_event_source_games(s)["resolved"] == 1
        g = s.get(Goal, gid)
        assert g.game_title == "Mr. Bean's Wacky World" and g.system == "Wii"


def test_resolve_event_source_game_aotw_desc_prefix(fresh_engine):
    # AotW clone descriptions carry a "[FF1] " prefix; normalization still matches the source.
    from app.services.goals import resolve_event_source_games
    with Session(engine) as s:
        s.add(RAAchievement(achievement_id=100, game_id=219, game_title="Final Fantasy",
                            console_id=7, console_name="NES", title="Troll Face",
                            description="Defeat the dark elf Astos", earned_at=datetime.utcnow(), hardcore=True))
        s.commit()
        g = Goal(game_title="AotW", system="Events", ra_game_id=37650, achievement_id=571351,
                 objective=GoalObjective.achievement, custom_text="Troll Face",
                 achievement_desc="[FF1] Defeat the dark elf Astos ", event_name="AotW")
        s.add(g); s.commit(); s.refresh(g)
        gid = g.id
    with Session(engine) as s:
        assert resolve_event_source_games(s)["resolved"] == 1
        assert s.get(Goal, gid).system == "NES"


def test_resolve_event_source_game_ambiguous_leaves_blank(fresh_engine):
    from app.services.goals import resolve_event_source_games
    with Session(engine) as s:
        # same name+desc across TWO real games → ambiguous → don't conclude
        s.add(RAAchievement(achievement_id=1, game_id=10, game_title="Game A", console_id=7,
                            console_name="NES", title="Win", description="Beat it",
                            earned_at=datetime.utcnow(), hardcore=True))
        s.add(RAAchievement(achievement_id=2, game_id=20, game_title="Game B", console_id=12,
                            console_name="SNES", title="Win", description="Beat it",
                            earned_at=datetime.utcnow(), hardcore=True))
        s.commit()
        g = Goal(game_title="Ev", system="Events", ra_game_id=999, achievement_id=99,
                 objective=GoalObjective.achievement, custom_text="Win", achievement_desc="Beat it",
                 event_name="Ev")
        s.add(g); s.commit(); s.refresh(g)
        gid = g.id
    with Session(engine) as s:
        assert resolve_event_source_games(s)["resolved"] == 0
        assert s.get(Goal, gid).system == "Events"   # left unresolved


def test_event_cards_sorted_achievements_first(fresh_engine):
    from app.routers.goals import _build_group, _card_ctx
    now = datetime.utcnow()
    g_master = Goal(game_title="A Game", system="Wii", ra_game_id=1,
                    objective=GoalObjective.master, event_name="E")
    g_ach = Goal(game_title="B Game", system="PS1", ra_game_id=2, achievement_id=9,
                 objective=GoalObjective.achievement, custom_text="Do it", event_name="E")
    g_custom = Goal(game_title="C Game", system="NES", ra_game_id=3,
                    objective=GoalObjective.custom, custom_text="finish", event_name="E")
    all_goals = [g_master, g_ach, g_custom]
    cards = [_card_ctx(g, {}, now) for g in all_goals]    # input order: master, ach, custom
    grp = _build_group("E", cards, all_goals, None, [])   # no sub-categories
    # All uncategorized → one section; achievements first within it.
    sec = [s for s in grp["sections"] if s["is_uncat"]][0]
    objs = [c["goal"].objective for c in sec["cards"]]
    assert objs[0] == GoalObjective.achievement           # achievements at the top
    assert GoalObjective.achievement not in objs[1:]      # full games (master/custom) at the bottom


def test_looks_like_image():
    from app.routers.goals import _looks_like_image
    assert _looks_like_image("https://i.imgur.com/abc.png")
    assert _looks_like_image("https://x/y.JPG?token=1")     # case + query ignored
    assert _looks_like_image("https://x/y.webp#frag")
    assert not _looks_like_image("https://docs.google.com/spreadsheets/d/abc")
    assert not _looks_like_image("")


def test_event_edit_sets_deadline_and_image_renders(client):
    with Session(engine) as s:
        s.add(GoalEvent(name="Summer", url="", auto_sync=False))
        s.add(Goal(game_title="G", system="NES", ra_game_id=1,
                   objective=GoalObjective.master, event_name="Summer"))
        s.commit()
    r = client.post("/goals/event/edit", data={
        "name": "Summer", "url": "https://i.imgur.com/pic.png", "deadline": "2026-09-01"})
    assert r.status_code == 200
    with Session(engine) as s:
        ev = s.exec(select(GoalEvent).where(GoalEvent.name == "Summer")).first()
        assert ev.url == "https://i.imgur.com/pic.png"
        assert ev.deadline is not None and ev.deadline.month == 9
    html = client.get("/goals").text
    assert "i.imgur.com/pic.png" in html      # image URL rendered as <img> in the header
    assert "Sep 01, 2026" in html             # event deadline badge


def test_event_edit_clears_deadline(client):
    with Session(engine) as s:
        s.add(GoalEvent(name="E", url="", deadline=datetime(2026, 5, 1), auto_sync=False))
        s.add(Goal(game_title="G", system="NES", ra_game_id=1,
                   objective=GoalObjective.master, event_name="E"))
        s.commit()
    client.post("/goals/event/edit", data={"name": "E", "url": "", "deadline": ""})
    with Session(engine) as s:
        assert s.exec(select(GoalEvent).where(GoalEvent.name == "E")).first().deadline is None


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


def test_evaluate_event_clone_completes_by_its_own_id(fresh_engine):
    """An imported event goal carries the EVENT-CLONE achievement_id (its own id, distinct
    from the source-game achievement). RA records the clone as its own hardcore unlock, so a
    plain id match completes the goal — trust the achievement id the import comes with."""
    with Session(engine) as s:
        # The event-clone unlock (id 571351) in the mirror, hardcore; the source-game
        # achievement (id 9001) is a separate, irrelevant row.
        s.add(RAAchievement(achievement_id=571351, game_id=37650, earned_at=datetime.utcnow(), hardcore=True))
        s.add(RAAchievement(achievement_id=9001, game_id=219, earned_at=datetime.utcnow(), hardcore=True))
        s.commit()
        clone = Goal(game_title="AotW 2026", ra_game_id=37650, achievement_id=571351,
                     objective=GoalObjective.achievement, custom_text="Troll Face",
                     event_name="Achievement of the Week 2026")
        s.add(clone); s.commit(); s.refresh(clone)
        clone_id = clone.id

    with Session(engine) as s:
        assert evaluate_goals(s)["completed"] == 1
        assert s.get(Goal, clone_id).status == GoalStatus.completed


def test_event_tally_stable_when_hiding_completed(fresh_engine):
    """Hiding completed (or past) goals must NOT change an event's total/done tally — only
    which cards render. Regression: the tally was computed from the filtered visible cards."""
    from app.routers.goals import _build_group, _card_ctx
    now = datetime.utcnow()
    done = Goal(game_title="G", system="PS1", ra_game_id=1, objective=GoalObjective.achievement,
                achievement_id=1, points=5, event_name="E", status=GoalStatus.completed)
    active = Goal(game_title="G", system="PS1", ra_game_id=1, objective=GoalObjective.achievement,
                  achievement_id=2, points=7, event_name="E", status=GoalStatus.active)
    all_goals = [done, active]
    visible_cards = [_card_ctx(active, {}, now)]   # show_completed OFF → completed card filtered out
    grp = _build_group("E", visible_cards, all_goals, None, [])
    assert grp["total"] == 2 and grp["done"] == 1            # tally from ALL goals, not visible
    assert grp["ach_total"] == 2 and grp["ach_done"] == 1
    assert grp["points_total"] == 12 and grp["points_done"] == 5
    visible_total = sum(len(s["cards"]) for s in grp["sections"])
    assert visible_total == 1                                # display still filtered


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


def test_copy_goal_clones_into_another_group(client):
    # A completed custom goal with display-text art, in an event + sub-category.
    with Session(engine) as s:
        g = Goal(game_title="Blob Attack", system="Arduboy", objective=GoalObjective.custom,
                 custom_text="Earn 100 xp", display_text="+100 XP", icon="✚", icon_color="#22c55e",
                 event_name="Challenge League 2026", category="Logram Gym 1: drisc",
                 status=GoalStatus.completed, completed_at=datetime.utcnow(), auto=False)
        s.add(g); s.commit(); s.refresh(g); gid = g.id

    r = client.post(f"/goals/{gid}/copy", data={
        "event_name": "Challenge League 2026", "category": "Antico Gym 1: Hotscrock"})
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"

    with Session(engine) as s:
        assert len(s.exec(select(Goal)).all()) == 2                  # original stays + the copy
        copy = s.exec(select(Goal).where(Goal.category == "Antico Gym 1: Hotscrock")).first()
        assert copy.id != gid
        assert copy.event_name == "Challenge League 2026"
        # tracking + art fields cloned …
        assert copy.custom_text == "Earn 100 xp" and copy.display_text == "+100 XP"
        assert copy.icon == "✚" and copy.icon_color == "#22c55e"
        # … but the copy starts fresh (not carried-over completed/auto).
        assert copy.status == GoalStatus.active and copy.auto is False and copy.completed_at is None
        assert s.get(Goal, gid).category == "Logram Gym 1: drisc"   # original untouched


def test_copy_goal_dedups_within_same_target_group(client):
    with Session(engine) as s:
        g = Goal(game_title="Gex", system="PlayStation", ra_game_id=500,
                 objective=GoalObjective.beaten, event_name="Challenge League 2026",
                 category="Logram Gym 1: drisc")
        s.add(g); s.commit(); s.refresh(g); gid = g.id
    # Copy to a different event …
    client.post(f"/goals/{gid}/copy", data={"event_name": "Collectathon", "category": ""})
    # … then copy to the SAME group again → de-duped (no third row).
    client.post(f"/goals/{gid}/copy", data={"event_name": "Collectathon", "category": ""})
    with Session(engine) as s:
        assert len(s.exec(select(Goal).where(Goal.event_name == "Collectathon")).all()) == 1
        assert len(s.exec(select(Goal)).all()) == 2   # original + one copy only


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


def test_api_import_event_json(client, monkeypatch):
    """Extension path: POST /api/import-event (JSON) imports an event's achievements."""
    from app.services.ra_client import RAClient
    payload = _extended(title="AotW 2026", achs=[_ach(1, "A"), _ach(2, "B", badge="00000"), _ach(3, "C")])

    async def fake(self, gid):
        return payload
    monkeypatch.setattr(RAClient, "get_game_extended", fake)
    _seed_creds()

    r = client.post("/api/import-event", json={"ra_game_id": 196, "include_completed": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["created"] == 2 and body["skipped_placeholder"] == 1
    with Session(engine) as s:
        goals = s.exec(select(Goal).where(Goal.ra_game_id == 196)).all()
        assert len(goals) == 2
        ev = s.exec(select(GoalEvent).where(GoalEvent.ra_game_id == 196)).first()
        assert ev.auto_sync is True and ev.name == "AotW 2026"


def test_api_import_event_parses_url(client, monkeypatch):
    from app.services.ra_client import RAClient

    async def fake(self, gid):
        assert gid == 196   # parsed from the /event/ URL
        return _extended(achs=[_ach(1, "A")])
    monkeypatch.setattr(RAClient, "get_game_extended", fake)
    _seed_creds()
    r = client.post("/api/import-event",
                    json={"event_ref": "https://retroachievements.org/event/196-achievement-of-the-week-2026"})
    assert r.json()["status"] == "ok"


def test_api_import_event_no_id(client):
    r = client.post("/api/import-event", json={"event_ref": "not a number"})
    assert r.json()["status"] == "error"


def test_events_search_by_name(client, monkeypatch):
    from app.services.ra_client import RAClient

    async def fake_search(self, query):
        assert query == "aotw"
        return [{"ID": 196, "Title": "Achievement of the Week 2026", "NumAchievements": 64}]
    monkeypatch.setattr(RAClient, "search_events", fake_search)
    _seed_creds()
    r = client.get("/ra/events/search?q=aotw")
    assert r.status_code == 200
    assert "Achievement of the Week 2026" in r.text
    assert "prepImportEvent" in r.text and "196" in r.text


def test_events_search_without_creds(client):
    r = client.get("/ra/events/search?q=x")
    assert r.status_code == 200 and "credentials" in r.text.lower()


# --- event deadline auto-pull (RA V2 activeThrough) ------------------------

def test_parse_iso_dt_formats():
    from app.services.events import _parse_iso_dt
    assert _parse_iso_dt("2027-01-03T00:00:00.000000Z").day == 3
    assert _parse_iso_dt("2027-01-03").month == 1
    assert _parse_iso_dt("") is None and _parse_iso_dt("not-a-date") is None


def test_import_event_pulls_deadline_from_v2(client, monkeypatch):
    from app.services.ra_client import RAClient
    from app.services.ra_client_v2 import RAClientV2
    _seed_creds()

    async def fake_ext(self, gid):
        return _extended(achs=[_ach(1, "A")])

    async def fake_event(self, event_id, include="awards"):
        return {"data": {"attributes": {"title": "AotW", "activeThrough": "2027-01-03T00:00:00.000000Z"}}}
    monkeypatch.setattr(RAClient, "get_game_extended", fake_ext)
    monkeypatch.setattr(RAClientV2, "get_event", fake_event)

    r = client.post("/api/import-event", json={"ra_game_id": 196})  # no deadline given
    assert r.json()["status"] == "ok"
    with Session(engine) as s:
        g = s.exec(select(Goal).where(Goal.ra_game_id == 196)).first()
        assert g.deadline is not None and (g.deadline.year, g.deadline.month, g.deadline.day) == (2027, 1, 3)
        ev = s.exec(select(GoalEvent).where(GoalEvent.ra_game_id == 196)).first()
        assert ev.deadline.year == 2027   # stored on the event for nightly-added goals


def test_import_event_explicit_deadline_wins(client, monkeypatch):
    # V2 is still consulted (for award tiers), but an explicit deadline is NOT overridden.
    from app.services.ra_client import RAClient
    from app.services.ra_client_v2 import RAClientV2
    _seed_creds()

    async def fake_ext(self, gid):
        return _extended(achs=[_ach(1, "A")])

    async def fake_event(self, event_id, include="awards"):
        return {"data": {"attributes": {"activeThrough": "2027-01-03"}}}  # would-be deadline
    monkeypatch.setattr(RAClient, "get_game_extended", fake_ext)
    monkeypatch.setattr(RAClientV2, "get_event", fake_event)

    client.post("/api/import-event", json={"ra_game_id": 196, "deadline": "2026-09-18"})
    with Session(engine) as s:
        g = s.exec(select(Goal).where(Goal.ra_game_id == 196)).first()
        assert (g.deadline.month, g.deadline.day) == (9, 18)   # explicit wins over V2's activeThrough


def test_v2_parsers_tiers_and_source_game():
    from app.services.ra_client_v2 import RAClientV2
    ev_payload = {"data": {"attributes": {"title": "AotW"}},
                  "included": [
                      {"type": "user-awards", "attributes": {"title": "Bronze", "kind": "bronze", "pointsRequired": 12, "badgeUrl": "b"}},
                      {"type": "user-awards", "attributes": {"title": "Champion", "kind": "champion", "pointsRequired": 64, "badgeUrl": "c"}},
                  ]}
    tiers = RAClientV2.tiers_from_event(ev_payload)
    assert [t["title"] for t in tiers] == ["Bronze", "Champion"]   # sorted by threshold
    assert tiers[0]["points_required"] == 12 and tiers[1]["badge_url"] == "c"

    ach_payload = {"data": {"relationships": {"games": {"data": [{"type": "games", "id": "777"}]}}},
                   "included": [
                       {"type": "games", "id": "777", "attributes": {"title": "Metal Arms"},
                        "relationships": {"system": {"data": {"type": "systems", "id": "16"}}}},
                       {"type": "systems", "id": "16", "attributes": {"name": "GameCube"}},
                   ]}
    src = RAClientV2.source_game_from_achievement(ach_payload)
    assert src == {"game_id": "777", "title": "Metal Arms", "console": "GameCube"}


def test_import_event_stores_tiers(client, monkeypatch):
    from app.services.ra_client import RAClient
    from app.services.ra_client_v2 import RAClientV2
    _seed_creds()

    async def fake_ext(self, gid):
        return _extended(achs=[_ach(1, "A")])

    async def fake_event(self, event_id, include="awards"):
        return {"data": {"attributes": {"activeThrough": "2027-01-03"}},
                "included": [{"type": "user-awards", "attributes": {"title": "Bronze", "pointsRequired": 12, "badgeUrl": "b"}}]}
    monkeypatch.setattr(RAClient, "get_game_extended", fake_ext)
    monkeypatch.setattr(RAClientV2, "get_event", fake_event)

    client.post("/api/import-event", json={"ra_game_id": 196})
    with Session(engine) as s:
        ev = s.exec(select(GoalEvent).where(GoalEvent.ra_game_id == 196)).first()
        import json as _json
        tiers = _json.loads(ev.tiers_json)
        assert tiers[0]["title"] == "Bronze" and tiers[0]["points_required"] == 12


def test_enrich_source_games_updates_goals(client, monkeypatch):
    import asyncio
    from app.services.ra_client_v2 import RAClientV2
    from app.services import events as events_service
    _seed_creds()
    with Session(engine) as s:
        s.add(GoalEvent(name="Ev", ra_game_id=196, auto_sync=True))
        s.add(Goal(game_title="Ev", system="Events", ra_game_id=196, achievement_id=5,
                   objective=GoalObjective.achievement, custom_text="Do thing"))
        s.commit()

    async def fake_ach(self, achievement_id, include="games.system"):
        return {"data": {"relationships": {"games": {"data": [{"type": "games", "id": "777"}]}}},
                "included": [
                    {"type": "games", "id": "777", "attributes": {"title": "Metal Arms"},
                     "relationships": {"system": {"data": {"type": "systems", "id": "16"}}}},
                    {"type": "systems", "id": "16", "attributes": {"name": "GameCube"}},
                ]}
    monkeypatch.setattr(RAClientV2, "get_achievement", fake_ach)

    asyncio.run(events_service.enrich_source_games(196))
    with Session(engine) as s:
        g = s.exec(select(Goal).where(Goal.achievement_id == 5)).first()
        assert g.game_title == "Metal Arms" and g.system == "GameCube"


def test_import_event_v2_unreachable_is_graceful(client, monkeypatch):
    from app.services.ra_client import RAClient
    from app.services.ra_client_v2 import RAClientV2
    _seed_creds()

    async def fake_ext(self, gid):
        return _extended(achs=[_ach(1, "A")])

    async def boom(self, event_id, include="awards"):
        raise RuntimeError("cloudflare 403")
    monkeypatch.setattr(RAClient, "get_game_extended", fake_ext)
    monkeypatch.setattr(RAClientV2, "get_event", boom)

    r = client.post("/api/import-event", json={"ra_game_id": 196})  # no deadline; V2 fails
    assert r.json()["status"] == "ok"   # import still succeeds
    with Session(engine) as s:
        g = s.exec(select(Goal).where(Goal.ra_game_id == 196)).first()
        assert g.deadline is None


# --- goals page filters / sort / delete-event ------------------------------

def _seed_filter_goals():
    from datetime import timedelta
    now = datetime.utcnow()
    with Session(engine) as s:
        s.add(Goal(game_title="DoneGoal", system="NES", ra_game_id=1, objective=GoalObjective.master,
                   status=GoalStatus.completed, completed_at=now))
        s.add(Goal(game_title="PastGoal", system="NES", ra_game_id=2, objective=GoalObjective.master,
                   deadline=now - timedelta(days=3)))   # overdue + incomplete
        s.add(Goal(game_title="FutureGoal", system="NES", ra_game_id=3, objective=GoalObjective.master,
                   deadline=now + timedelta(days=3)))
        s.commit()


def test_goals_default_hides_past_shows_completed(client):
    _seed_filter_goals()
    html = client.get("/goals").text
    assert "DoneGoal" in html        # completed shown by default
    assert "FutureGoal" in html
    assert "PastGoal" not in html    # past-deadline hidden by default
    assert "Showing 2 of 3" in html


def test_goals_hide_completed(client):
    _seed_filter_goals()
    html = client.get("/goals?show_completed=0").text
    assert "DoneGoal" not in html
    assert "FutureGoal" in html


def test_goals_show_past(client):
    _seed_filter_goals()
    html = client.get("/goals?show_past=1").text
    assert "PastGoal" in html
    assert "Showing 3 of 3" in html


def test_goals_sort_renders(client):
    _seed_filter_goals()
    for sort in ("event", "due", "added", "title"):
        assert client.get(f"/goals?sort={sort}&show_past=1").status_code == 200


def test_delete_whole_event(client):
    with Session(engine) as s:
        s.add(GoalEvent(name="Doomed Event", auto_sync=False))
        s.add(Goal(game_title="A", ra_game_id=10, achievement_id=1, objective=GoalObjective.achievement,
                   event_name="Doomed Event"))
        s.add(Goal(game_title="B", ra_game_id=10, achievement_id=2, objective=GoalObjective.achievement,
                   event_name="Doomed Event"))
        s.commit()
    r = client.post("/goals/event/delete", data={"name": "Doomed Event"})
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    with Session(engine) as s:
        assert s.exec(select(Goal).where(Goal.event_name == "Doomed Event")).all() == []
        assert s.exec(select(GoalEvent).where(GoalEvent.name == "Doomed Event")).first() is None


def test_game_goal_greyscale_is_persistent_not_hover(client):
    # Game goals greyscale the cover until done; must NOT clear on hover/tap (mobile bug).
    with Session(engine) as s:
        s.add(Goal(game_title="Locked", system="NES", ra_game_id=1,
                   objective=GoalObjective.master, cover_path="covers/1.png"))
        s.commit()
    html = client.get("/goals").text
    assert "grayscale" in html
    assert "group-hover:grayscale-0" not in html   # no hover/tap reveal


def test_active_achievement_goal_uses_locked_badge_and_links(client):
    # Achievement goals swap to the LOCKED badge (no greyscale) while active, and link to source.
    with Session(engine) as s:
        s.add(Goal(game_title="Final Fantasy", system="NES", ra_game_id=219, achievement_id=571351,
                   objective=GoalObjective.achievement, custom_text="Troll Face",
                   cover_path="https://media.retroachievements.org/Badge/193454.png"))
        s.commit()
    html = client.get("/goals").text
    assert "Badge/193454_lock.png" in html                       # locked image while active
    assert "retroachievements.org/achievement/571351" in html    # achievement title → source
    assert "retroachievements.org/game/219" in html              # game name → source


def test_completed_achievement_goal_uses_unlocked_badge(client):
    with Session(engine) as s:
        s.add(Goal(game_title="FF", system="NES", ra_game_id=219, achievement_id=571351,
                   objective=GoalObjective.achievement, custom_text="Troll Face",
                   cover_path="https://media.retroachievements.org/Badge/193454.png",
                   status=GoalStatus.completed))
        s.commit()
    html = client.get("/goals").text
    assert "Badge/193454.png" in html
    assert "Badge/193454_lock.png" not in html   # unlocked image when done


# --- sub-categories, failed status, custom display (new) -------------------

def test_render_notes_safe_markdown():
    from app.routers.goals import _render_notes
    assert _render_notes("") == ""
    out = _render_notes("**b** *i* `c` <script>")
    assert "<strong>b</strong>" in out and "<em>i</em>" in out
    assert "&lt;script&gt;" in out                       # HTML escaped (XSS-safe)
    link = _render_notes("[g](https://x.com)")
    assert 'href="https://x.com"' in link and ">g</a>" in link


def test_fail_hides_by_default_and_show_failed_reveals(client):
    with Session(engine) as s:
        g = Goal(game_title="Doomed", system="NES", objective=GoalObjective.custom,
                 custom_text="do it", event_name="E")
        s.add(g); s.commit(); s.refresh(g); gid = g.id
    r = client.post(f"/goals/{gid}/fail")
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(Goal, gid).status == GoalStatus.failed
    assert "✗ Failed" not in client.get("/goals").text              # hidden by default
    assert "✗ Failed" in client.get("/goals?show_failed=1").text    # revealed
    # reopen un-fails
    client.post(f"/goals/{gid}/reopen")
    with Session(engine) as s:
        assert s.get(Goal, gid).status == GoalStatus.active


def test_failed_goal_off_event_tally(fresh_engine):
    from app.routers.goals import _build_group, _card_ctx
    now = datetime.utcnow()
    done = Goal(game_title="A", system="NES", objective=GoalObjective.custom, custom_text="x",
                event_name="E", status=GoalStatus.completed)
    failed = Goal(game_title="B", system="NES", objective=GoalObjective.custom, custom_text="x",
                  event_name="E", status=GoalStatus.failed)
    active = Goal(game_title="C", system="NES", objective=GoalObjective.custom, custom_text="x",
                  event_name="E", status=GoalStatus.active)
    all_goals = [done, failed, active]
    cards = [_card_ctx(active, {}, now)]   # failed + completed hidden
    grp = _build_group("E", cards, all_goals, None, [])
    assert grp["total"] == 2 and grp["done"] == 1     # failed excluded from the tally
    assert grp["failed_count"] == 1


def test_category_crud_and_notes_rendered(client):
    with Session(engine) as s:
        g = Goal(game_title="G", system="NES", objective=GoalObjective.custom, custom_text="x",
                 event_name="Ev", category="Week 1")
        s.add(g); s.commit()
    r = client.post("/goals/category", data={"event_name": "Ev", "name": "Week 1",
                                             "deadline": "2030-01-01", "notes": "**important**"})
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    page = client.get("/goals").text
    assert "Week 1" in page and "<strong>important</strong>" in page
    assert "★" in page          # the tinted-icon picker renders on the page (not just on re-render)
    # rename re-points the goal
    client.post("/goals/category/edit", data={"event_name": "Ev", "old_name": "Week 1",
                                              "name": "Week One", "deadline": "", "notes": ""})
    with Session(engine) as s:
        assert s.exec(select(Goal).where(Goal.category == "Week One")).first() is not None
        assert s.exec(select(GoalCategory).where(GoalCategory.name == "Week One")).first() is not None
    # delete reverts the goal to uncategorized (not deleted)
    client.post("/goals/category/delete", data={"event_name": "Ev", "name": "Week One"})
    with Session(engine) as s:
        assert s.exec(select(GoalCategory)).first() is None
        g = s.exec(select(Goal).where(Goal.game_title == "G")).first()
        assert g is not None and g.category == ""


def test_sections_interleave_by_due_date(fresh_engine):
    from app.routers.goals import _build_group, _card_ctx
    now = datetime.utcnow()
    far = datetime(2030, 1, 1)
    near = datetime(2026, 1, 1)
    g_cat = Goal(game_title="A", system="NES", objective=GoalObjective.custom, custom_text="c",
                 event_name="E", category="Late")
    g_uncat = Goal(game_title="B", system="NES", objective=GoalObjective.custom, custom_text="c",
                   event_name="E", deadline=near)
    all_goals = [g_cat, g_uncat]
    cards = [_card_ctx(g, {}, now) for g in all_goals]
    cat = GoalCategory(event_name="E", name="Late", deadline=far)
    grp = _build_group("E", cards, all_goals, None, [cat])
    keys = [s["key"] for s in grp["sections"]]
    assert keys.index("") < keys.index("Late")    # near-due uncategorized before the far-dated category


def test_goal_edit_sets_custom_display(client):
    with Session(engine) as s:
        g = Goal(game_title="G", system="NES", objective=GoalObjective.custom, custom_text="x",
                 event_name="Ev")
        s.add(g); s.commit(); s.refresh(g); gid = g.id
    client.post(f"/goals/{gid}/edit", data={"event_name": "Ev", "category": "Cat", "deadline": "",
                                            "display_text": "+100 XP", "icon": "★", "icon_color": "#ff0000"})
    with Session(engine) as s:
        g = s.get(Goal, gid)
        assert g.category == "Cat" and g.display_text == "+100 XP"
        assert g.icon == "★" and g.icon_color == "#ff0000"
    # an icon not in the curated set is rejected
    client.post(f"/goals/{gid}/edit", data={"event_name": "Ev", "category": "", "deadline": "",
                                            "display_text": "", "icon": "💀", "icon_color": ""})
    with Session(engine) as s:
        assert s.get(Goal, gid).icon == ""
