"""RetroAchievements dashboard — local-mirror sync + query helpers.

The dashboard is driven entirely by a LOCAL copy of the configured user's RA data,
so browsing/filtering/graphing makes zero RA calls. `refresh()` re-pulls everything
and REPLACES the mirror tables in a transaction — that is how retroactively-changed
achievements (repointed/removed/demoted, backdated unlocks) reconcile; the mirror is
never append-only.

All RA calls go through ra_client's shared 2 req/s limiter. A full backfill is a few
dozen calls (member-since → now in windows + a couple of completion pages), so a
manual refresh takes a minute or two and is respectful of the personal API key.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil

from sqlmodel import Session, select, text

from app.db.database import engine
from app.db.models import RAAchievement, RAGameProgress, RAProfile, LibraryEntry
from app.services import settings as app_settings
from app.services import logger as applog
from app.services import activity as activity_store
from app.services.ra_client import RAClient

_SYNC_ID = "ra-sync"


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _epoch(dt: datetime) -> int:
    """UTC Unix seconds for a naive (UTC-assumed) datetime."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _int(v) -> int:
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


async def refresh() -> dict:
    """Full re-pull + replace of the dashboard mirror. Returns a summary dict.

    Safe to re-run any time; each run rebuilds the mirror from scratch so RA-side
    retroactive changes are absorbed rather than drifting.
    """
    with Session(engine) as s:
        username = app_settings.get(s, "ra_username")
        api_key = app_settings.get(s, "ra_api_key")
        try:
            window_days = int(app_settings.get(s, "ra_dashboard_window_days", "60") or "60")
        except ValueError:
            window_days = 60
    if not (username and api_key):
        return {"status": "no_credentials"}

    ra = RAClient(username, api_key)
    activity_store.start(_SYNC_ID, "Syncing RetroAchievements…", task_type="task")
    try:
        # 1. Profile (for headline stats + member-since to bound the backfill).
        profile = await ra.get_user_profile()
        member_since = _parse_dt(profile.get("MemberSince")) or datetime(2012, 1, 1)
        now = datetime.utcnow()

        # 2. Earned achievements: member_since → now in windows. De-dupe on
        #    (achievement_id, hardcore) so window-boundary overlaps don't double-count
        #    while still keeping separate softcore + hardcore unlocks.
        window = timedelta(days=max(1, window_days))
        total_windows = max(1, ceil((now - member_since) / window))
        earned: dict[tuple[int, bool], dict] = {}
        cur, i = member_since, 0
        while cur < now:
            nxt = min(cur + window, now)
            i += 1
            activity_store.update_label(_SYNC_ID, f"Syncing achievements… window {i}/{total_windows}")
            for a in await ra.get_achievements_earned_between(_epoch(cur), _epoch(nxt)):
                key = (_int(a.get("AchievementID")), bool(_int(a.get("HardcoreMode"))))
                earned.setdefault(key, a)
            cur = nxt

        # 3. Per-game completion (paginated).
        activity_store.update_label(_SYNC_ID, "Syncing game completion…")
        progress_rows: list[dict] = []
        offset = 0
        while True:
            page = await ra.get_user_completion_progress(count=500, offset=offset)
            results = page.get("Results", []) if isinstance(page, dict) else []
            progress_rows.extend(results)
            total = _int(page.get("Total")) if isinstance(page, dict) else 0
            offset += 500
            if offset >= total or not results:
                break

        # 4. Awards (mastery count for the profile tile).
        try:
            awards = await ra.get_user_awards()
        except Exception:
            awards = {}

        # 5. Replace the mirror in one transaction (retroactive reconciliation).
        with Session(engine) as s:
            owned_ids = {
                e.ra_game_id for e in
                s.exec(select(LibraryEntry).where(LibraryEntry.ra_game_id != None)).all()  # noqa: E711
            }
            s.exec(text("DELETE FROM ra_achievement"))
            s.exec(text("DELETE FROM ra_game_progress"))

            hardcore_unlocks = 0
            for a in earned.values():
                ts = _parse_dt(a.get("Date"))
                if ts is None:
                    continue
                hc = bool(_int(a.get("HardcoreMode")))
                hardcore_unlocks += 1 if hc else 0
                s.add(RAAchievement(
                    achievement_id=_int(a.get("AchievementID")),
                    title=a.get("Title", "") or "",
                    description=a.get("Description", "") or "",
                    points=_int(a.get("Points")),
                    true_ratio=_int(a.get("TrueRatio")),
                    type=(a.get("Type") or "") or "",
                    game_id=_int(a.get("GameID")),
                    game_title=a.get("GameTitle", "") or "",
                    console_id=_int(a.get("ConsoleID")),
                    console_name=a.get("ConsoleName", "") or "",
                    badge_url=a.get("BadgeURL", "") or "",
                    earned_at=ts,
                    hardcore=hc,
                ))

            games_with_progress = 0
            for g in progress_rows:
                gid = _int(g.get("GameID"))
                maxp = _int(g.get("MaxPossible"))
                awarded = _int(g.get("NumAwarded"))
                if awarded > 0:
                    games_with_progress += 1
                s.add(RAGameProgress(
                    game_id=gid,
                    title=g.get("Title", "") or "",
                    console_id=_int(g.get("ConsoleID")),
                    console_name=g.get("ConsoleName", "") or "",
                    image_icon=g.get("ImageIcon", "") or "",
                    max_possible=maxp,
                    num_awarded=awarded,
                    num_awarded_hardcore=_int(g.get("NumAwardedHardcore")),
                    pct_complete=round(100.0 * awarded / maxp, 1) if maxp else 0.0,
                    highest_award_kind=g.get("HighestAwardKind", "") or "",
                    highest_award_date=_parse_dt(g.get("HighestAwardDate")),
                    most_recent_date=_parse_dt(g.get("MostRecentAwardedDate")),
                    owned=gid in owned_ids,
                ))

            prof = s.get(RAProfile, 1) or RAProfile(id=1)
            prof.username = profile.get("User", username) or username
            prof.points = _int(profile.get("TotalPoints"))
            prof.points_softcore = _int(profile.get("TotalSoftcorePoints"))
            prof.rank = _int(profile.get("Rank"))
            prof.total_achievements = hardcore_unlocks
            prof.total_games = games_with_progress
            prof.total_masteries = _int(awards.get("MasteryAwardsCount")) if isinstance(awards, dict) else 0
            prof.member_since = member_since
            prof.last_synced_at = now
            s.add(prof)
            s.commit()

        ach_count, game_count = len(earned), len(progress_rows)
        with Session(engine) as s:
            app_settings.set(s, "ra_dashboard_last_sync", now.isoformat())
        applog.info("system", "RA dashboard synced", {"achievements": ach_count, "games": game_count})
        return {"status": "ok", "achievements": ach_count, "games": game_count}
    except Exception as exc:
        applog.warning("system", f"RA dashboard sync failed: {exc}")
        return {"status": "error", "error": str(exc)}
    finally:
        activity_store.finish(_SYNC_ID)


# ---------------------------------------------------------------------------
# Dashboard query helpers — pure local reads (no RA calls). Chart series are
# returned as JSON-ready lists for ApexCharts.
# ---------------------------------------------------------------------------

def get_profile(session: Session) -> RAProfile | None:
    return session.get(RAProfile, 1)


def overview(session: Session) -> dict:
    """Headline tiles + Overview charts, computed from the local mirror."""
    profile = session.get(RAProfile, 1)
    achs = session.exec(select(RAAchievement)).all()
    games = session.exec(select(RAGameProgress)).all()

    pts_by_month: dict[str, int] = defaultdict(int)
    cnt_by_month: dict[str, int] = defaultdict(int)
    hardcore = softcore = 0
    for a in achs:
        m = a.earned_at.strftime("%Y-%m")
        pts_by_month[m] += a.points
        cnt_by_month[m] += 1
        hardcore += 1 if a.hardcore else 0
        softcore += 0 if a.hardcore else 1

    months = sorted(pts_by_month)
    cum, points_series = 0, []
    for m in months:
        cum += pts_by_month[m]
        points_series.append({"x": m, "y": cum})
    per_month = [{"x": m, "y": cnt_by_month[m]} for m in months]

    owned = [g for g in games if g.owned]
    owned_mastered = sum(1 for g in owned if g.highest_award_kind in ("mastered", "completed"))
    recent = sorted(achs, key=lambda a: a.earned_at, reverse=True)[:12]

    return {
        "profile": profile,
        "points_series": points_series,
        "per_month": per_month,
        "hardcore": hardcore,
        "softcore": softcore,
        "owned_count": len(owned),
        "owned_mastered": owned_mastered,
        "recent": recent,
        "mirror_achievements": len(achs),
    }


def _console_names(session: Session) -> list[str]:
    rows = session.exec(select(RAAchievement.console_name).distinct()).all()
    return sorted({r for r in rows if r})


def timeline(session: Session, date_from: datetime | None = None, date_to: datetime | None = None,
             q: str = "", console: str = "", hardcore: bool | None = None, limit: int = 1000) -> dict:
    """Filtered unlock list + per-day activity series. All filters are optional."""
    stmt = select(RAAchievement)
    if date_from:
        stmt = stmt.where(RAAchievement.earned_at >= date_from)
    if date_to:
        stmt = stmt.where(RAAchievement.earned_at <= date_to)
    if q:
        stmt = stmt.where(RAAchievement.title.ilike(f"%{q}%"))
    if console:
        stmt = stmt.where(RAAchievement.console_name == console)
    if hardcore is not None:
        stmt = stmt.where(RAAchievement.hardcore == hardcore)
    rows = session.exec(stmt.order_by(RAAchievement.earned_at.desc())).all()

    by_day: dict[str, int] = defaultdict(int)
    pts = 0
    for a in rows:
        by_day[a.earned_at.strftime("%Y-%m-%d")] += 1
        pts += a.points
    activity = [{"x": d, "y": by_day[d]} for d in sorted(by_day)]
    return {
        "rows": rows[:limit],
        "total": len(rows),
        "points": pts,
        "activity": activity,
        "consoles": _console_names(session),
    }
