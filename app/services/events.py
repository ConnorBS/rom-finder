"""Event import + nightly sync for goals.

An "event" groups goals under a name. An RA-sourced event maps to an RA event/game
hub (its achievements are pulled via API_GetGameExtended in ONE call) and can auto-sync
nightly to pick up newly-added achievements (AotW, random rolls, etc.). A custom event
is just a named group with an optional URL (e.g. a Google Sheet) for navigation.

Placeholder achievement tiles (RA BadgeName "00000") are skipped on import/sync.
"""
import re
from datetime import datetime

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import Goal, GoalEvent, GoalObjective, RAAchievement
from app.services import settings as app_settings
from app.services import logger as applog
from app.services.goals import evaluate_goals
from app.services.ra_client import RAClient

_ID_RE = re.compile(r"(?:/(?:game|event)/)?(\d+)")


def parse_event_ref(text: str) -> int | None:
    """Pull an RA game/event id from a pasted URL or a bare number.
    Accepts '12345', '.../game/12345', '.../event/12345' (with optional query/hash)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    m = re.search(r"/(?:game|event)/(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _creds() -> tuple[str, str]:
    with Session(engine) as s:
        return app_settings.get(s, "ra_username"), app_settings.get(s, "ra_api_key")


def _parse_iso_dt(value) -> datetime | None:
    """Parse an RA V2 ISO timestamp (e.g. '2027-01-03T00:00:00.000000Z' or
    '2027-01-03') to a naive UTC datetime. Returns None on anything unparseable."""
    if not value:
        return None
    s = str(value).strip().rstrip("Zz")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


async def fetch_event_deadline(api_key: str, event_id: int) -> datetime | None:
    """Best-effort: the RA **V2** event's `activeThrough` (its end date) → the goal
    deadline. Returns None if V2 is unreachable/unauthorized (Cloudflare, bad key, no
    such field) so import never fails on it — it just doesn't auto-set a deadline.
    Activates automatically once V2 is reachable (verify with /api/diag/ra-v2)."""
    from app.services.ra_client_v2 import RAClientV2
    try:
        data = await RAClientV2(api_key).get_event(event_id)
    except Exception as exc:
        applog.verbose("system", f"V2 event deadline lookup skipped (event {event_id}): {exc}", {})
        return None
    attrs = (data.get("data") or {}).get("attributes", {}) if isinstance(data, dict) else {}
    return _parse_iso_dt(attrs.get("activeThrough"))


def build_event_goals(
    session: Session,
    *,
    ra_game_id: int,
    event_name: str,
    game_title: str,
    system: str,
    achievements: list[dict],
    include_completed: bool,
    deadline: datetime | None,
) -> dict:
    """Create an achievement Goal for each of the event's achievements (LOCAL, no
    commit — the caller commits). Skips placeholder tiles, already-tracked
    achievements, and — when include_completed is False — ones already earned in
    hardcore. Returns per-category counts."""
    existing = {
        g.achievement_id for g in session.exec(
            select(Goal).where(Goal.ra_game_id == ra_game_id, Goal.achievement_id != None)  # noqa: E711
        ).all()
    }
    earned_hc = {
        a.achievement_id for a in session.exec(
            select(RAAchievement).where(RAAchievement.hardcore == True)  # noqa: E712
        ).all()
    }
    created = skipped_existing = skipped_placeholder = skipped_done = 0
    for a in achievements:
        aid = a.get("id")
        if aid is None:
            continue
        if not a.get("badge_url"):           # placeholder tile (BadgeName 00000) → ignore
            skipped_placeholder += 1
            continue
        if aid in existing:
            skipped_existing += 1
            continue
        if not include_completed and aid in earned_hc:
            skipped_done += 1
            continue
        session.add(Goal(
            game_title=game_title,
            system=system,
            ra_game_id=ra_game_id,
            achievement_id=aid,
            objective=GoalObjective.achievement,
            custom_text=a.get("title", ""),
            achievement_desc=a.get("description", ""),
            points=a.get("points", 0) or 0,      # the achievement's own RA points (NOT event points)
            cover_path=a.get("badge_url", ""),   # absolute badge URL
            event_name=event_name,
            deadline=deadline,
        ))
        existing.add(aid)
        created += 1
    return {
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_placeholder": skipped_placeholder,
        "skipped_done": skipped_done,
    }


def upsert_event(session: Session, name: str, **fields) -> GoalEvent:
    """Create or update a GoalEvent by name; only provided (non-None) fields are set."""
    ev = session.exec(select(GoalEvent).where(GoalEvent.name == name)).first()
    if ev is None:
        ev = GoalEvent(name=name)
    for k, v in fields.items():
        if v is not None:
            setattr(ev, k, v)
    ev.updated_at = datetime.utcnow()
    session.add(ev)
    return ev


async def sync_event(
    game_id: int,
    *,
    event_name: str | None = None,
    deadline: datetime | None = None,
    include_completed: bool = True,
    auto_sync: bool = True,
) -> dict:
    """Fetch an RA event/game hub's achievements (ONE API call) and import any not
    already tracked, then record/refresh its GoalEvent. Used by the import endpoint
    and the nightly sync. Never holds a DB session across the RA await."""
    username, api_key = _creds()
    if not (username and api_key):
        return {"error": "no_credentials"}
    try:
        data = await RAClient(username, api_key).get_game_extended(game_id)
    except Exception as exc:
        applog.warning("system", f"Event sync fetch failed (game {game_id}): {exc}")
        return {"error": str(exc)}

    title = data.get("Title") or f"Game #{game_id}"
    system = data.get("ConsoleName", "")
    achievements = RAClient.achievements_from_extended(data)
    name = (event_name or title).strip() or title

    # No explicit deadline? Best-effort: use the event's end date (V2 `activeThrough`).
    if deadline is None:
        with Session(engine) as s:
            pull = app_settings.get_bool(s, "event_pull_deadline", True)
        if pull:
            deadline = await fetch_event_deadline(api_key, game_id)

    with Session(engine) as s:
        stats = build_event_goals(
            s, ra_game_id=game_id, event_name=name, game_title=title, system=system,
            achievements=achievements, include_completed=include_completed, deadline=deadline,
        )
        upsert_event(
            s, name, ra_game_id=game_id, auto_sync=auto_sync,
            include_completed=include_completed, deadline=deadline,
            last_synced_at=datetime.utcnow(),
        )
        s.commit()
        evaluate_goals(s)   # flip any imported-as-already-earned to completed

    stats.update({"event": name, "total_achievements": len(achievements)})
    if stats["created"]:
        applog.info("system", f"Event '{name}' imported", stats)
    return stats


async def sync_all_auto() -> dict:
    """Nightly: re-check every auto-sync RA event for new achievements. One RA call
    per event (rate-limited globally to 2 req/s), so it never floods the server."""
    with Session(engine) as s:
        jobs = [
            (e.ra_game_id, e.name, e.include_completed, e.deadline)
            for e in s.exec(
                select(GoalEvent).where(GoalEvent.auto_sync == True, GoalEvent.ra_game_id != None)  # noqa: E711,E712
            ).all()
        ]
    created = 0
    for ra_game_id, name, include_completed, deadline in jobs:
        res = await sync_event(ra_game_id, event_name=name, deadline=deadline,
                               include_completed=include_completed, auto_sync=True)
        created += res.get("created", 0)
    if created:
        applog.info("scheduler", "Event sync added new achievement goals", {"events": len(jobs), "created": created})
    return {"events": len(jobs), "created": created}
