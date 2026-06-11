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
    """Last path segment of a badge URL (e.g. '193454.png'), lowercased; '' for empty or a
    placeholder badge ('0'/'00000'). Used by the `/api/diag/goal-mirror` diagnostic to report
    badge overlap; matching itself is by achievement_id (see evaluate_goals)."""
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
    `completed_at` is the REAL RA date (the achievement's hardcore unlock, or the game's
    beat/mastery date), NOT when this evaluator happened to run. Also self-heals the
    completed_at of already-auto-completed goals (which predate this). Returns counts."""
    rows = {r.game_id: r for r in session.exec(select(RAGameProgress)).all()}
    # Hardcore-earned achievement id → EARLIEST hardcore earn date. Event-clone achievements
    # (AotW/Roulette) are their OWN distinct hardcore unlocks with their own id (NOT the
    # source-game id), so a plain id match is correct — the import stores that same clone id.
    ach_date: dict[int, datetime] = {}
    for a in session.exec(select(RAAchievement).where(RAAchievement.hardcore == True)).all():  # noqa: E712
        cur = ach_date.get(a.achievement_id)
        if cur is None or (a.earned_at and a.earned_at < cur):
            ach_date[a.achievement_id] = a.earned_at

    def _unlock_date(g: Goal) -> datetime | None:
        """The real RA date the goal was satisfied — the achievement's hardcore unlock, or the
        game's beat/mastery date — NOT the evaluator's run time."""
        if g.objective == GoalObjective.achievement:
            return ach_date.get(g.achievement_id)
        row = rows.get(g.ra_game_id) if g.ra_game_id is not None else None
        return (row.highest_award_date or row.most_recent_date) if row else None

    now = datetime.utcnow()
    flipped = 0
    # 1) Flip newly-satisfied active goals, stamping the REAL unlock/beat date.
    for g in session.exec(select(Goal).where(
            Goal.status == GoalStatus.active, Goal.objective != GoalObjective.custom)).all():
        if g.objective == GoalObjective.achievement:
            done = g.achievement_id is not None and g.achievement_id in ach_date
        else:
            row = rows.get(g.ra_game_id) if g.ra_game_id is not None else None
            done = bool(row and award_satisfies(g.objective, row.highest_award_kind))
        if done:
            g.status = GoalStatus.completed
            g.auto = True
            g.completed_at = _unlock_date(g) or now
            g.updated_at = now
            session.add(g)
            flipped += 1

    # 2) Self-heal already-auto-completed goals whose completed_at was stamped with the sync
    #    time (pre-fix). Re-stamp from the mirror's real date when it differs; idempotent.
    corrected = 0
    for g in session.exec(select(Goal).where(
            Goal.status == GoalStatus.completed, Goal.auto == True,  # noqa: E712
            Goal.objective != GoalObjective.custom)).all():
        real = _unlock_date(g)
        if real and g.completed_at != real:
            g.completed_at = real
            g.updated_at = now
            session.add(g)
            corrected += 1

    if flipped or corrected:
        session.commit()
        if flipped:
            applog.info("system", "Goals auto-completed", {"completed": flipped})
        if corrected:
            applog.info("system", "Goal completion dates corrected to RA unlock date", {"corrected": corrected})
    return {"completed": flipped, "corrected": corrected}
