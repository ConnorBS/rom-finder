"""Goal auto-completion against the LOCAL RA mirror (zero RA calls).

A master/beaten goal flips to completed when the matched game's RAGameProgress
award satisfies it — **hardcore only** (softcore awards never count). Custom goals
never auto-complete (the user marks them done). Run after a dashboard refresh and
on every Goals page load — the only moments the mirror can change.
"""
from datetime import datetime

from sqlmodel import Session, select

from app.db.models import Goal, GoalObjective, GoalStatus, RAAchievement, RAGameProgress
from app.services import logger as applog


def _badge_key(url: str) -> str:
    """Last path segment of a badge URL (e.g. '193454.png'), lowercased; '' for empty
    or a placeholder badge ('0'/'00000'). Lets an event achievement goal match the SOURCE
    achievement the user actually earns: RA's AotW / RA Roulette event tiles are CLONES
    with their own achievement_id under the event hub, but they reuse the source
    achievement's badge image. The user unlocks the source (a different id), so id-only
    matching never completes those goals — the shared badge is the reliable link."""
    if not url:
        return ""
    seg = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if seg.split(".", 1)[0] in ("", "0", "00000"):
        return ""
    return seg


def award_satisfies(objective: str, kind: str) -> bool:
    """True when an RA award tier (highest_award_kind) satisfies the goal. Hardcore
    only: a softcore award (beaten-softcore / completed) never counts."""
    if objective == GoalObjective.master:
        return kind == "mastered"
    if objective == GoalObjective.beaten:
        return kind in ("beaten", "mastered")
    return False  # custom / achievement use a different signal (see evaluate_goals)


def evaluate_goals(session: Session) -> dict:
    """Flip active, auto-trackable goals to completed when the local RA mirror satisfies
    them — LOCAL (no RA calls). master/beaten read the per-game award tier; achievement
    goals are done once the achievement is unlocked in HARDCORE. Custom never auto-flips.
    Returns {"completed": N} newly flipped."""
    rows = {r.game_id: r for r in session.exec(select(RAGameProgress)).all()}
    # Hardcore-earned achievement ids + their badge keys (the mirror dedupes on
    # (achievement_id, hardcore)). Badges back the event-clone fallback below.
    earned_hc: set[int] = set()
    earned_hc_badges: set[str] = set()
    for a in session.exec(select(RAAchievement).where(RAAchievement.hardcore == True)).all():  # noqa: E712
        earned_hc.add(a.achievement_id)
        bk = _badge_key(a.badge_url)
        if bk:
            earned_hc_badges.add(bk)
    goals = session.exec(
        select(Goal).where(
            Goal.status == GoalStatus.active,
            Goal.objective != GoalObjective.custom,
        )
    ).all()
    flipped = 0
    for g in goals:
        done = False
        if g.objective == GoalObjective.achievement:
            done = g.achievement_id is not None and g.achievement_id in earned_hc
            # Event clones (AotW / RA Roulette) carry a hub-specific achievement_id the
            # user never directly unlocks; fall back to a hardcore badge match (the clone
            # reuses the source achievement's badge). Scoped to event-grouped goals so a
            # direct single-achievement goal can't false-match on a reused badge image.
            if not done and g.event_name:
                done = _badge_key(g.cover_path) in earned_hc_badges
        elif g.ra_game_id is not None:
            row = rows.get(g.ra_game_id)
            done = bool(row and award_satisfies(g.objective, row.highest_award_kind))
        if done:
            g.status = GoalStatus.completed
            g.auto = True
            g.completed_at = g.updated_at = datetime.utcnow()
            session.add(g)
            flipped += 1
    if flipped:
        session.commit()
        applog.info("system", "Goals auto-completed", {"completed": flipped})
    return {"completed": flipped}
