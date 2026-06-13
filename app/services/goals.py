"""Goal auto-completion against the LOCAL RA mirror (zero RA calls).

A master/beaten goal flips to completed when the matched game's RAGameProgress
award satisfies it — **hardcore only** (softcore awards never count). Custom goals
never auto-complete (the user marks them done). Run after a dashboard refresh and
on every Goals page load — the only moments the mirror can change.
"""
import re
from datetime import datetime

from sqlmodel import Session, select

from app.db.models import Goal, GoalObjective, GoalStatus, RAAchievement, RAGameProgress
from app.services import logger as applog

_DESC_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")  # leading event game tag, e.g. "[FF1] "


def _match_key(title: str, desc: str) -> tuple[str, str] | None:
    """Normalized (name, description) key for matching an event-clone achievement to its
    SOURCE. Strips a leading event game tag from the description (AotW clones prefix it),
    trims + lowercases. Returns None unless BOTH are present — we only conclude a source
    game when the name AND the description match."""
    t = (title or "").strip().lower()
    d = _DESC_TAG.sub("", (desc or "")).strip().lower()
    return (t, d) if (t and d) else None


def resolve_event_source_games(session: Session) -> dict:
    """LOCAL: resolve an event achievement goal's REAL source game + console. An event
    clone's V2 `games` relationship points at the event hub, not the real game — so instead
    we match the achievement's NAME + DESCRIPTION against the mirror's NON-event achievements
    (which carry the real game + console). Exactly one distinct source game → set it; zero or
    AMBIGUOUS (>1 game) → leave the goal unresolved. Zero RA calls. Only touches event-hub
    goals (`system == "Events"`), so once resolved they're skipped."""
    pending = session.exec(select(Goal).where(
        Goal.objective == GoalObjective.achievement, Goal.system == "Events")).all()
    if not pending:
        return {"resolved": 0}

    index: dict[tuple[str, str], set[tuple[int, str, str]]] = {}
    for a in session.exec(select(RAAchievement)).all():
        if a.console_id == 101 or (a.console_name or "").strip().lower() == "events":
            continue   # skip event-console clones — we want the SOURCE game
        if not (a.game_id and a.game_title):
            continue
        key = _match_key(a.title, a.description)
        if key:
            index.setdefault(key, set()).add((a.game_id, a.game_title, a.console_name or ""))

    resolved = 0
    for g in pending:
        key = _match_key(g.custom_text, g.achievement_desc)
        if not key:
            continue
        matches = index.get(key, set())
        if len({m[0] for m in matches}) == 1:   # exactly one distinct source game
            _, game_title, console = next(iter(matches))
            g.game_title = game_title
            if console:
                g.system = console
            g.updated_at = datetime.utcnow()
            session.add(g)
            resolved += 1
        # 0 or >1 distinct games → leave blank (unresolved)
    if resolved:
        session.commit()
        applog.info("system", "Event source games resolved (local name+desc match)", {"resolved": resolved})
    return {"resolved": resolved}


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
    only: a softcore award (beaten-softcore / completed) never counts. NB: RA's API
    returns **"beaten-hardcore"** for a hardcore beat (not plain "beaten"), so both
    spellings must count — otherwise hardcore-beaten goals never auto-complete."""
    if objective == GoalObjective.master:
        return kind == "mastered"
    if objective == GoalObjective.beaten:
        return kind in ("beaten", "beaten-hardcore", "mastered")
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
