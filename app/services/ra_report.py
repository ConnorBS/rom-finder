"""Generate RetroAchievements forum-markup reports from the local mirror.

RA forums render Markdown (headers, **bold**, tables, lists) plus RA shortcodes:
`[game=ID]`, `[ach=ID]`, `[user=NAME]`, `[spoiler]...[/spoiler]`. We use shortcodes
for game/achievement references in list/heading contexts (they render to rich links)
and plain titles inside tables (shortcode-in-table rendering is unreliable).

All data comes from the local mirror via ra_dashboard — no RA calls.
"""
from collections import Counter
from datetime import datetime

from sqlmodel import Session, select

from app.db.models import RAAchievement, RAGameProgress, RAProfile
from app.services import ra_dashboard


def _game_tag(game_id: int, title: str) -> str:
    return f"[game={game_id}]" if game_id else (title or "Unknown game")


def _ach_tag(ach_id: int, title: str) -> str:
    return f"[ach={ach_id}]" if ach_id else (title or "achievement")


def _user_tag(name: str) -> str:
    return f"[user={name}]" if name else "this user"


def _span_label(date_from: datetime | None, date_to: datetime | None) -> str:
    if date_from and date_to:
        return f"{date_from:%Y-%m-%d} → {date_to:%Y-%m-%d}"
    if date_from:
        return f"since {date_from:%Y-%m-%d}"
    if date_to:
        return f"through {date_to:%Y-%m-%d}"
    return "all time"


def _slice(session: Session, heading: str, date_from=None, date_to=None,
           q: str = "", console: str = "", hardcore: bool | None = None) -> str:
    """Shared engine for the time-period recap + custom-view reports."""
    data = ra_dashboard.timeline(session, date_from, date_to, q, console, hardcore, limit=1_000_000)
    rows = data["rows"]
    total = len(rows)
    points = sum(a.points for a in rows)
    games = {a.game_id for a in rows}

    out = [heading, ""]
    out.append(
        f"Earned **{total:,} achievements** worth **{points:,} points**"
        + (f" across **{len(games):,} games**." if total else ".")
    )
    if not rows:
        return "\n".join(out)

    g_cnt: Counter = Counter()
    g_pts: Counter = Counter()
    g_name: dict[int, str] = {}
    for a in rows:
        g_cnt[a.game_id] += 1
        g_pts[a.game_id] += a.points
        g_name[a.game_id] = a.game_title
    out += ["", "### Top games", "", "| Game | Achievements | Points |", "|---|--:|--:|"]
    for gid, n in g_cnt.most_common(10):
        out.append(f"| {g_name.get(gid) or 'Unknown'} | {n} | {g_pts[gid]:,} |")

    rarest = sorted(rows, key=lambda a: a.true_ratio, reverse=True)[:5]
    out += ["", "### Rarest unlocks", ""]
    for a in rarest:
        out.append(f"- {_ach_tag(a.achievement_id, a.title)} **{a.title}** "
                   f"({a.game_title}) — TrueRatio {a.true_ratio}")
    return "\n".join(out)


def time_period_recap(session: Session, date_from=None, date_to=None) -> str:
    prof = session.get(RAProfile, 1)
    who = (_user_tag(prof.username) + " — ") if prof and prof.username else "My "
    return _slice(session, f"## {who}RetroAchievements: {_span_label(date_from, date_to)}",
                  date_from, date_to)


def custom_view(session: Session, date_from=None, date_to=None,
                q: str = "", console: str = "", hardcore: bool | None = None) -> str:
    bits = []
    if q:
        bits.append(f"“{q}”")
    if console:
        bits.append(console)
    if hardcore is True:
        bits.append("hardcore")
    elif hardcore is False:
        bits.append("softcore")
    bits.append(_span_label(date_from, date_to))
    return _slice(session, f"## RetroAchievements — {', '.join(bits)}",
                  date_from, date_to, q, console, hardcore)


def lifetime_showcase(session: Session) -> str:
    prof = session.get(RAProfile, 1)
    ins = ra_dashboard.insights(session)
    head = (_user_tag(prof.username) + "'s") if prof and prof.username else "My"
    out = [f"## {head} RetroAchievements profile", ""]
    if prof:
        rank = f" · rank #{prof.rank:,}" if prof.rank else ""
        out.append(f"- **{prof.points:,}** points (hardcore){rank}")
        out.append(f"- **{prof.total_achievements:,}** achievements across **{prof.total_games:,}** games")
        out.append(f"- **{prof.total_masteries:,}** masteries")
    if ins["by_console"]:
        out += ["", "### Top consoles", "", "| Console | Achievements | Points | Masteries |",
                "|---|--:|--:|--:|"]
        for c in ins["by_console"][:10]:
            out.append(f"| {c['console']} | {c['achievements']:,} | {c['points']:,} | {c['masteries']} |")
    return "\n".join(out)


def per_game(session: Session, game_id: int) -> str:
    g = session.exec(select(RAGameProgress).where(RAGameProgress.game_id == game_id)).first()
    achs = session.exec(
        select(RAAchievement).where(RAAchievement.game_id == game_id)
        .order_by(RAAchievement.earned_at)
    ).all()
    if not g and not achs:
        return "_No data for that game in the mirror — try a Refresh._"
    title = g.title if g else (achs[0].game_title if achs else "Game")
    out = [f"## {_game_tag(game_id, title)}", ""]
    if g:
        award = f" — **{g.highest_award_kind}**" if g.highest_award_kind else ""
        out.append(f"Progress: **{g.num_awarded}/{g.max_possible}** ({g.pct_complete}%){award}")
    out += ["", "### Achievements earned", ""]
    for a in achs:
        hc = " *(hardcore)*" if a.hardcore else ""
        out.append(f"- {_ach_tag(a.achievement_id, a.title)} **{a.title}** — "
                   f"{a.points}pts, {a.earned_at:%Y-%m-%d}{hc}")
    return "\n".join(out)


def build(session: Session, report_type: str, date_from=None, date_to=None,
          game_id: int = 0, q: str = "", console: str = "", hardcore: bool | None = None) -> str:
    """Dispatch to the right builder. report_type: recap|lifetime|per_game|custom."""
    if report_type == "lifetime":
        return lifetime_showcase(session)
    if report_type == "per_game":
        return per_game(session, game_id)
    if report_type == "custom":
        return custom_view(session, date_from, date_to, q, console, hardcore)
    return time_period_recap(session, date_from, date_to)
