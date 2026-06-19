import html as _html
import json
import re
from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks, Query, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from pathlib import Path
from datetime import datetime

from app.db.database import get_session, engine
from app.db.models import Goal, GoalEvent, GoalCategory, GoalObjective, GoalStatus, RAGameProgress
from app.services import settings as app_settings
from app.services import logger as applog
from app.services import events as events_service
from app.services.goals import evaluate_goals, resolve_event_source_games
from app.services.ra_client import SYSTEMS, RAClient

router = APIRouter(prefix="/goals")
templates = Jinja2Templates(directory="app/templates")

# Curated tintable glyphs for the "text instead of image" goal display — picked to be
# text-presentation (so a chosen colour applies), shown centered below the display text.
GOAL_ICONS = ["★", "☆", "✦", "✪", "❂", "✿", "❀", "◆", "●",
              "♥", "♠", "♣", "♦", "♛", "✚", "❄", "✸", "⬢"]
_ALLOWED_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _ra_configured(session: Session) -> bool:
    return bool(app_settings.get(session, "ra_username") and app_settings.get(session, "ra_api_key"))


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".svg")


def _looks_like_image(url: str) -> bool:
    """True when a URL points at an image (by extension, ignoring any query/fragment) so the
    event header can render it inline instead of a text link."""
    if not url:
        return False
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    return path.endswith(_IMAGE_EXTS)


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _render_notes(text: str) -> str:
    """Tiny SAFE markdown for category notes → HTML string ("" when empty, so no element
    renders). Escapes first, then **bold** / *italic* / `code` / [label](http…) / newlines."""
    text = (text or "").strip()
    if not text:
        return ""
    out = _html.escape(text)
    out = _MD_LINK.sub(
        r'<a href="\2" target="_blank" rel="noopener" class="text-blue-400 hover:underline">\1</a>', out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"`([^`]+)`", r'<code class="bg-gray-800 px-1 rounded text-gray-300">\1</code>', out)
    return out.replace("\n", "<br>")


def _parse_deadline(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _category_names_for_event(session: Session, event_name: str) -> list[str]:
    """Sub-category names belonging to ONE event (its GoalCategory rows + any names goals
    reference) — so a goal card's category dropdown is scoped to its own event, not global."""
    names = {c.name for c in session.exec(
        select(GoalCategory).where(GoalCategory.event_name == event_name)).all()}
    names |= {g.category for g in session.exec(
        select(Goal).where(Goal.event_name == event_name)).all() if g.category}
    return sorted(names)


def _card_ctx(goal: Goal, progress_by_id: dict, now: datetime, event_cats: list | None = None) -> dict:
    """Per-goal render context: the matched RA progress row + deadline state + the icon set +
    the goal's own event's sub-category names (the per-event category datalist)."""
    progress = progress_by_id.get(goal.ra_game_id) if goal.ra_game_id else None
    overdue = bool(goal.status == GoalStatus.active and goal.deadline and goal.deadline < now)
    days_left = (goal.deadline.date() - now.date()).days if goal.deadline else None
    return {"goal": goal, "progress": progress, "overdue": overdue, "days_left": days_left,
            "now": now, "goal_icons": GOAL_ICONS, "event_categories": event_cats or []}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def goals_page(
    request: Request,
    show_completed: str = Query(default="1"),
    show_past: str = Query(default="0"),
    show_failed: str = Query(default="0"),
    sort: str = Query(default="due"),
    session: Session = Depends(get_session),
):
    evaluate_goals(session)            # LOCAL — fold in any RA progress since last visit
    resolve_event_source_games(session)  # LOCAL — name+desc match → real source game/console

    show_completed_on = show_completed != "0"
    show_past_on = show_past == "1"
    show_failed_on = show_failed == "1"
    if sort not in ("event", "due", "added", "title"):
        sort = "due"

    all_goals = session.exec(select(Goal)).all()
    progress_by_id = {r.game_id: r for r in session.exec(select(RAGameProgress)).all()}
    event_rows = {ev.name: ev for ev in session.exec(select(GoalEvent)).all()}
    cats_by_event: dict[str, list] = {}
    for c in session.exec(select(GoalCategory)).all():
        cats_by_event.setdefault(c.event_name or "", []).append(c)
    now = datetime.utcnow()

    # Filters (counts of what's hidden surface in the header). Failed is checked first so a
    # failed+overdue goal is counted as failed-hidden, not past-hidden.
    hidden_completed = hidden_past = hidden_failed = 0
    visible: list[Goal] = []
    for g in all_goals:
        is_done = g.status == GoalStatus.completed
        is_failed = g.status == GoalStatus.failed
        is_past = bool(g.status == GoalStatus.active and g.deadline and g.deadline < now)
        if is_failed and not show_failed_on:
            hidden_failed += 1
            continue
        if is_done and not show_completed_on:
            hidden_completed += 1
            continue
        if is_past and not show_past_on:
            hidden_past += 1
            continue
        visible.append(g)

    # Per-card sort key (applied within each event group / sub-section).
    _far = datetime.max
    def _card_key(g: Goal):
        if sort == "due":
            return (g.deadline or _far, g.game_title or "", g.id)
        if sort == "title":
            return ((g.custom_text or g.game_title or "").lower(), g.id)
        if sort == "added":
            return (g.created_at or _far, g.id)
        return (g.created_at or _far, g.id)  # 'event' → stable by creation within the group
    visible.sort(key=_card_key)

    # Per-event sub-category names (event-scoped datalist for each card's category field).
    cat_names_by_event: dict[str, list] = {}
    for c in session.exec(select(GoalCategory)).all():
        cat_names_by_event.setdefault(c.event_name or "", []).append(c.name)
    for g in all_goals:
        if g.category:
            cat_names_by_event.setdefault(g.event_name or "", []).append(g.category)
    cat_names_by_event = {k: sorted(set(v)) for k, v in cat_names_by_event.items()}

    grouped: dict[str, list] = {}
    for g in visible:
        grouped.setdefault(g.event_name or "", []).append(
            _card_ctx(g, progress_by_id, now, cat_names_by_event.get(g.event_name or "", [])))
    # ALL goals per event (filter-independent) so an event's tally/total never moves when
    # completed/past/failed goals are hidden — only which cards render changes.
    all_by_event: dict[str, list] = {}
    for g in all_goals:
        all_by_event.setdefault(g.event_name or "", []).append(g)
    # Events that exist (via a GoalEvent row or a category) but have no visible cards.
    extra = set(event_rows) | set(cats_by_event)
    empty_events = [name for name in extra if name and name not in grouped]
    event_keys = [e for e in grouped if e] + sorted(empty_events)

    # Event-group ordering.
    def _event_min_due(name: str):
        ds = [g.deadline for g in all_by_event.get(name, []) if g.deadline and g.status != GoalStatus.failed]
        ev = event_rows.get(name)
        if ev and ev.deadline:
            ds.append(ev.deadline)
        ds += [c.deadline for c in cats_by_event.get(name, []) if c.deadline]
        return min(ds) if ds else _far
    if sort in ("due",):
        event_keys.sort(key=_event_min_due)
    elif sort == "title":
        event_keys.sort(key=lambda n: n.lower())
    # 'event'/'added' keep first-seen order.
    ordered_events = event_keys + ([""] if "" in grouped else [])

    groups = [_build_group(e, grouped.get(e, []), all_by_event.get(e, []),
                           event_rows.get(e), cats_by_event.get(e, []))
              for e in ordered_events]
    event_names = sorted(set([g.event_name for g in all_goals if g.event_name]) | set(event_rows))

    applog.log_navigation("goals", {"goal_count": len(all_goals), "events": len(event_rows)})
    return templates.TemplateResponse(
        request, "goals.html",
        {
            "groups": groups,
            "count": len(all_goals),
            "visible_count": len(visible),
            "hidden_completed": hidden_completed,
            "hidden_past": hidden_past,
            "hidden_failed": hidden_failed,
            "show_completed_on": show_completed_on,
            "show_past_on": show_past_on,
            "show_failed_on": show_failed_on,
            "sort": sort,
            "event_names": event_names,
            "goal_icons": GOAL_ICONS,
            "systems": sorted(SYSTEMS.items(), key=lambda x: x[1]),
            "ra_configured": _ra_configured(session),
            "now": now,
        },
    )


def _section_cards(cards: list) -> list:
    """Stable achievements-first ordering within a sub-section (keeps the page sort)."""
    return sorted(cards, key=lambda c: 0 if c["goal"].objective == GoalObjective.achievement else 1)


def _build_group(name: str, cards: list, all_goals: list, event: GoalEvent | None,
                 categories: list) -> dict:
    """Assemble one event group, split into SUB-SECTIONS (categories + an uncategorized
    section), each ordered by closest due date. TALLIES (done/total, achievements, points,
    tier) come from `all_goals` (every goal in the event, EXCLUDING failed) so hiding
    completed/past/failed cards never changes the totals. `categories` is the event's
    GoalCategory rows."""
    _far = datetime.max
    live = [g for g in all_goals if g.status != GoalStatus.failed]   # failed = abandoned, off the tally
    ach = [g for g in live if g.objective == GoalObjective.achievement]
    points_done = sum(g.points for g in ach if g.status == GoalStatus.completed)

    # RA V2 award tiers (Bronze→Champion): parse the cached JSON + mark the current tier
    # = highest whose pointsRequired the user's earned event points have reached.
    tiers = []
    current_tier = None
    if event and event.tiers_json:
        try:
            tiers = json.loads(event.tiers_json) or []
        except (ValueError, TypeError):
            tiers = []
        for t in tiers:
            pr = t.get("points_required")
            t["reached"] = bool(pr is not None and points_done >= pr)
            if t["reached"]:
                current_tier = t

    # --- Sub-sections: one per category + an uncategorized section --------------
    cat_by_name = {c.name: c for c in categories}
    cards_by_cat: dict[str, list] = {}
    for c in cards:
        cards_by_cat.setdefault(c["goal"].category or "", []).append(c)
    live_by_cat: dict[str, list] = {}
    for g in live:
        live_by_cat.setdefault(g.category or "", []).append(g)

    # Every category name that exists as a GoalCategory row OR is referenced by a goal.
    cat_names = set(cat_by_name) | {g.category for g in live if g.category}
    sections = []
    for cn in cat_names:
        cat = cat_by_name.get(cn)
        live_in = live_by_cat.get(cn, [])
        sections.append({
            "is_uncat": False, "title": cn, "key": cn,
            "deadline": cat.deadline if cat else None,
            "notes_html": _render_notes(cat.notes) if cat else "",
            "category": cat,
            "cards": _section_cards(cards_by_cat.get(cn, [])),
            "done": sum(1 for g in live_in if g.status == GoalStatus.completed),
            "total": len(live_in),
        })
    uncat_live = live_by_cat.get("", [])
    uncat_cards = cards_by_cat.get("", [])
    if uncat_live or uncat_cards:
        uds = [g.deadline for g in uncat_live if g.deadline]
        sections.append({
            "is_uncat": True, "title": "", "key": "",
            "deadline": min(uds) if uds else None, "notes_html": "", "category": None,
            "cards": _section_cards(uncat_cards),
            "done": sum(1 for g in uncat_live if g.status == GoalStatus.completed),
            "total": len(uncat_live),
        })
    # Categories AND the uncategorized games interleave by closest due date (None last).
    sections.sort(key=lambda s: (s["deadline"] or _far, 1 if s["is_uncat"] else 0, s["title"].lower()))

    return {
        "name": name,
        "sections": sections,
        "has_categories": bool(cat_names),
        "done": sum(1 for g in live if g.status == GoalStatus.completed),
        "total": len(live),
        "failed_count": sum(1 for g in all_goals if g.status == GoalStatus.failed),
        "ach_total": len(ach),
        "ach_done": sum(1 for g in ach if g.status == GoalStatus.completed),
        "points_total": sum(g.points for g in ach),
        "points_done": points_done,
        "tiers": tiers,
        "current_tier": current_tier,
        "event": event,
        "event_image": bool(event and event.url and _looks_like_image(event.url)),
    }


# ---------------------------------------------------------------------------
# Add / edit / complete / delete
# ---------------------------------------------------------------------------

@router.post("/add", response_class=HTMLResponse)
async def add_goal(
    request: Request,
    background_tasks: BackgroundTasks,
    ra_game_id: int = Form(...),
    game_title: str = Form(...),
    system: str = Form(...),
    objective: str = Form(default=GoalObjective.beaten),
    achievement_id: str = Form(default=""),
    achievement_title: str = Form(default=""),
    event_name: str = Form(default=""),
    deadline: str = Form(default=""),
    session: Session = Depends(get_session),
):
    # An achievement_id (set when the user picked a specific achievement off the
    # game's list) makes this an achievement goal regardless of the objective select.
    ach_id = int(achievement_id) if achievement_id.strip().isdigit() else None
    if ach_id is not None:
        objective = GoalObjective.achievement
    elif objective not in (GoalObjective.master, GoalObjective.beaten):
        objective = GoalObjective.beaten

    goal = Goal(
        game_title=game_title,
        system=system,
        ra_game_id=ra_game_id,
        achievement_id=ach_id,
        objective=objective,
        custom_text=achievement_title.strip() if ach_id is not None else "",
        event_name=event_name.strip(),
        deadline=_parse_deadline(deadline),
    )
    # Reuse a cover already on disk for this RA id (fetched by wanted/library).
    cover_file = Path(app_settings.get(session, "covers_dir", "static/covers")) / f"{ra_game_id}.png"
    if cover_file.exists() and ach_id is None:
        goal.cover_path = f"covers/{ra_game_id}.png"
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("add_goal", {
        "game": game_title, "system": system, "ra_game_id": ra_game_id,
        "objective": objective, "achievement_id": ach_id, "event": goal.event_name, "id": goal.id,
    })
    if ach_id is not None:
        # Pull the achievement's title/description/badge from the RA API for a rich card.
        background_tasks.add_task(_enrich_achievement_goal, goal.id, ra_game_id, ach_id)
    elif not goal.cover_path:
        background_tasks.add_task(_fetch_cover_goal, goal.id, ra_game_id, game_title, system)
    return HTMLResponse(
        '<span class="text-green-400 text-xs">✓ Goal added.</span>'
        '<a href="/goals" class="text-blue-400 text-xs hover:underline ml-2">View ↗</a>'
    )


@router.post("/add-custom", response_class=HTMLResponse)
async def add_custom_goal(
    game_title: str = Form(...),
    system: str = Form(default=""),
    custom_text: str = Form(...),
    event_name: str = Form(default=""),
    deadline: str = Form(default=""),
    session: Session = Depends(get_session),
):
    goal = Goal(
        game_title=game_title,
        system=system.strip(),
        objective=GoalObjective.custom,
        custom_text=custom_text.strip(),
        event_name=event_name.strip(),
        deadline=_parse_deadline(deadline),
    )
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("add_goal_custom", {
        "game": game_title, "objective": custom_text, "event": goal.event_name, "id": goal.id,
    })
    return HTMLResponse(
        '<span class="text-green-400 text-xs">✓ Goal added.</span>'
        '<a href="/goals" class="text-blue-400 text-xs hover:underline ml-2">View ↗</a>'
    )


@router.post("/import-event", response_class=HTMLResponse)
async def import_event(
    background_tasks: BackgroundTasks,
    event_ref: str = Form(...),
    event_name: str = Form(default=""),
    deadline: str = Form(default=""),
    include_completed: str = Form(default="false"),
):
    """Bulk-import every achievement of an RA event/game hub as goals — ONE RA API
    call. Async with NO Depends(get_session): events.sync_event manages its own
    sessions around the RA await. Records a GoalEvent (auto-sync on) so the nightly
    task grows it as new achievements land."""
    game_id = events_service.parse_event_ref(event_ref)
    if not game_id:
        return HTMLResponse('<span class="text-red-400 text-xs">Couldn\'t read an RA game/event id from that.</span>')
    inc = include_completed in ("true", "on", "1")
    res = await events_service.sync_event(
        game_id, event_name=(event_name.strip() or None),
        deadline=_parse_deadline(deadline), include_completed=inc, auto_sync=True,
    )
    # Resolve each imported achievement's real source game/console in the background (V2).
    if not res.get("error"):
        background_tasks.add_task(events_service.enrich_source_games, game_id)
    if res.get("error") == "no_credentials":
        return HTMLResponse('<span class="text-yellow-500 text-xs">Add RA credentials in Settings first.</span>')
    if res.get("error"):
        return HTMLResponse(f'<span class="text-red-400 text-xs">Import failed: {res["error"]}</span>')
    msg = (f'✓ Imported {res["created"]} of {res["total_achievements"]} achievement(s) for “{res["event"]}”'
           f' — {res["skipped_existing"]} already tracked, {res["skipped_done"]} already done,'
           f' {res["skipped_placeholder"]} unpublished/placeholder tiles skipped'
           f' (these are upcoming weeks — the nightly sync adds them once RA publishes a badge).')
    return HTMLResponse(
        f'<span class="text-green-400 text-xs">{msg}</span>'
        '<a href="/goals" class="text-blue-400 text-xs hover:underline ml-2">View ↗</a>'
    )


@router.post("/event", response_class=HTMLResponse)
async def upsert_custom_event(
    name: str = Form(...),
    url: str = Form(default=""),
    deadline: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Create or update a CUSTOM event — a named group with an optional link
    (e.g. a Google Sheet) + deadline. No RA involvement."""
    name = name.strip()
    if not name:
        return HTMLResponse('<span class="text-red-400 text-xs">Event name required.</span>')
    events_service.upsert_event(session, name, url=url.strip(), deadline=_parse_deadline(deadline), auto_sync=False)
    session.commit()
    applog.log_action("upsert_event", {"name": name, "url": url.strip()})
    return HTMLResponse(
        f'<span class="text-green-400 text-xs">✓ Event “{name}” saved.</span>'
        '<a href="/goals" class="text-blue-400 text-xs hover:underline ml-2">View ↗</a>'
    )


@router.post("/event/edit", response_class=HTMLResponse)
async def edit_event(
    name: str = Form(...),
    url: str = Form(default=""),
    deadline: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Update an event's link + deadline (custom OR RA) without touching its goals or
    `auto_sync`. Empty clears. Creates the GoalEvent if the group exists only via
    `goal.event_name`. Returns HX-Refresh so the header re-renders."""
    name = name.strip()
    ev = session.exec(select(GoalEvent).where(GoalEvent.name == name)).first()
    if ev is None:
        ev = GoalEvent(name=name)
    ev.url = url.strip()
    ev.deadline = _parse_deadline(deadline)
    ev.updated_at = datetime.utcnow()
    session.add(ev)
    session.commit()
    applog.log_action("edit_event", {"name": name})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/event/delete", response_class=HTMLResponse)
async def delete_event(name: str = Form(...), session: Session = Depends(get_session)):
    """Delete a whole event: every goal under it + its GoalEvent record."""
    name = name.strip()
    goals = session.exec(select(Goal).where(Goal.event_name == name)).all()
    for g in goals:
        session.delete(g)
    ev = session.exec(select(GoalEvent).where(GoalEvent.name == name)).first()
    if ev:
        session.delete(ev)
    session.commit()
    applog.log_action("delete_event", {"name": name, "goals_deleted": len(goals)})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


# ---------------------------------------------------------------------------
# Sub-categories within an event
# ---------------------------------------------------------------------------

def _parse_ra_id(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


@router.post("/category", response_class=HTMLResponse)
async def create_category(
    background_tasks: BackgroundTasks,
    event_name: str = Form(...),
    name: str = Form(default=""),
    deadline: str = Form(default=""),
    notes: str = Form(default=""),
    ra_game_id: str = Form(default=""),
    system: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Create (or update) a sub-category within an event. Optionally back it with an RA game
    (by id or manual search): the category takes the game's title (when no title was typed),
    console, box art, and a link to its RA page."""
    event_name = event_name.strip()
    name = name.strip()
    ra_id = _parse_ra_id(ra_game_id)
    if not name and ra_id is None:
        return HTMLResponse('<span class="text-red-400 text-xs">Sub-category title (or an RA game) required.</span>')
    # Without a typed title, a placeholder stands in until background enrichment fills the game name.
    if not name:
        name = f"Game #{ra_id}"
    cat = session.exec(select(GoalCategory).where(
        GoalCategory.event_name == event_name, GoalCategory.name == name)).first()
    if cat is None:
        cat = GoalCategory(event_name=event_name, name=name)
    cat.deadline = _parse_deadline(deadline)
    cat.notes = notes.strip()
    cat.ra_game_id = ra_id
    cat.system = system.strip() if ra_id is not None else ""
    cat.cover_path = cat.cover_path if ra_id is not None else ""
    cat.updated_at = datetime.utcnow()
    session.add(cat)
    session.commit()
    session.refresh(cat)
    applog.log_action("create_category", {"event": event_name, "name": name, "ra_game_id": ra_id})
    if ra_id is not None:
        background_tasks.add_task(_enrich_category, cat.id, ra_id, name.startswith("Game #"))
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/category/edit", response_class=HTMLResponse)
async def edit_category(
    background_tasks: BackgroundTasks,
    event_name: str = Form(...),
    old_name: str = Form(...),
    name: str = Form(...),
    deadline: str = Form(default=""),
    notes: str = Form(default=""),
    ra_game_id: str = Form(default=""),
    system: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Edit a sub-category — rename (re-points its goals to the new name), date, notes, and the
    optional backing RA game (attach/clear by id or manual search)."""
    event_name = event_name.strip()
    old_name = old_name.strip()
    name = name.strip() or old_name
    ra_id = _parse_ra_id(ra_game_id)
    cat = session.exec(select(GoalCategory).where(
        GoalCategory.event_name == event_name, GoalCategory.name == old_name)).first()
    if cat is None:
        cat = GoalCategory(event_name=event_name, name=name)
    cat.name = name
    cat.deadline = _parse_deadline(deadline)
    cat.notes = notes.strip()
    game_changed = (ra_id != cat.ra_game_id)
    cat.ra_game_id = ra_id
    cat.system = system.strip() if ra_id is not None else ""
    if ra_id is None:
        cat.cover_path = ""              # game detached → drop its box art
    cat.updated_at = datetime.utcnow()
    session.add(cat)
    if name != old_name:
        for g in session.exec(select(Goal).where(
                Goal.event_name == event_name, Goal.category == old_name)).all():
            g.category = name
            session.add(g)
    session.commit()
    session.refresh(cat)
    applog.log_action("edit_category", {"event": event_name, "name": name, "ra_game_id": ra_id})
    if ra_id is not None and game_changed:
        background_tasks.add_task(_enrich_category, cat.id, ra_id, False)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/category/delete", response_class=HTMLResponse)
async def delete_category(
    event_name: str = Form(...),
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    """Delete a sub-category; its goals revert to uncategorized (not deleted)."""
    event_name = event_name.strip()
    name = name.strip()
    cat = session.exec(select(GoalCategory).where(
        GoalCategory.event_name == event_name, GoalCategory.name == name)).first()
    if cat:
        session.delete(cat)
    for g in session.exec(select(Goal).where(
            Goal.event_name == event_name, Goal.category == name)).all():
        g.category = ""
        session.add(g)
    session.commit()
    applog.log_action("delete_category", {"event": event_name, "name": name})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/refresh-art", response_class=HTMLResponse)
async def refresh_art(background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
    """Re-pull achievement badges + game box art for all goals. Deduped by game and
    throttled by the global 2 req/s RA limiter, so it never floods the server."""
    if not _ra_configured(session):
        return HTMLResponse('<span class="text-yellow-500 text-xs">Add RA credentials in Settings first.</span>')
    background_tasks.add_task(_refresh_goal_art)
    return HTMLResponse('<span class="text-blue-400 text-xs">↻ Refreshing art in the background — watch the activity tray.</span>')


@router.post("/{goal_id}/complete", response_class=HTMLResponse)
async def complete_goal(request: Request, goal_id: int, session: Session = Depends(get_session)):
    goal = session.get(Goal, goal_id)
    if not goal:
        return HTMLResponse("")
    goal.status = GoalStatus.completed
    goal.auto = False
    goal.completed_at = goal.updated_at = datetime.utcnow()
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("complete_goal", {"id": goal_id, "game": goal.game_title})
    return _render_card(request, goal, session)


@router.post("/{goal_id}/reopen", response_class=HTMLResponse)
async def reopen_goal(request: Request, goal_id: int, session: Session = Depends(get_session)):
    goal = session.get(Goal, goal_id)
    if not goal:
        return HTMLResponse("")
    goal.status = GoalStatus.active
    goal.auto = False
    goal.completed_at = None
    goal.updated_at = datetime.utcnow()
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("reopen_goal", {"id": goal_id, "game": goal.game_title})
    return _render_card(request, goal, session)


@router.post("/{goal_id}/fail", response_class=HTMLResponse)
async def fail_goal(request: Request, goal_id: int, session: Session = Depends(get_session)):
    """Mark a goal failed — hidden unless 'Show failed', rendered with a red ✗ overlay.
    Reopen un-fails it."""
    goal = session.get(Goal, goal_id)
    if not goal:
        return HTMLResponse("")
    goal.status = GoalStatus.failed
    goal.auto = False
    goal.completed_at = None
    goal.updated_at = datetime.utcnow()
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("fail_goal", {"id": goal_id, "game": goal.game_title})
    return _render_card(request, goal, session)


@router.post("/{goal_id}/edit", response_class=HTMLResponse)
async def edit_goal(
    request: Request,
    goal_id: int,
    event_name: str = Form(default=""),
    category: str = Form(default=""),
    deadline: str = Form(default=""),
    custom_text: str = Form(default=""),
    display_text: str = Form(default=""),
    icon: str = Form(default=""),
    icon_color: str = Form(default=""),
    session: Session = Depends(get_session),
):
    goal = session.get(Goal, goal_id)
    if not goal:
        return HTMLResponse("")
    goal.event_name = event_name.strip()
    goal.category = category.strip()
    goal.deadline = _parse_deadline(deadline)
    if goal.objective == GoalObjective.custom and custom_text.strip():
        goal.custom_text = custom_text.strip()
    goal.display_text = display_text.strip()
    goal.icon = icon if icon in GOAL_ICONS else ""
    goal.icon_color = icon_color.strip()
    goal.updated_at = datetime.utcnow()
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("edit_goal", {"id": goal_id, "event": goal.event_name, "category": goal.category})
    return _render_card(request, goal, session)


# Fields cloned when copying a goal to another event / sub-category. The tracking fields
# (objective / ra_game_id / achievement_id / custom_text / …) are kept identical so the
# copy still auto-completes from the RA mirror; the copy starts `active` (auto-goals
# self-heal to completed on the next evaluate_goals pass). event_name / category / status /
# auto / completed_at / timestamps / custom_image are set explicitly, not cloned.
_COPY_FIELDS = (
    "game_title", "system", "ra_game_id", "achievement_id", "cover_path",
    "objective", "custom_text", "achievement_desc", "points", "deadline",
    "display_text", "icon", "icon_color",
)


@router.post("/{goal_id}/copy", response_class=HTMLResponse)
async def copy_goal(
    goal_id: int,
    event_name: str = Form(default=""),
    category: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Clone a goal into another event / sub-category, leaving the original in place.
    No-ops if an identical goal already lives in the target group (so copying onto itself,
    or re-copying, can't create an exact duplicate). Returns HX-Refresh so the new card
    lands in its proper group."""
    src = session.get(Goal, goal_id)
    if not src:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    event_name = event_name.strip()
    category = category.strip()
    # Don't create an exact duplicate within the SAME target group (NULL id/ach compares
    # render as IS NULL, so custom goals are matched on their text instead).
    existing = session.exec(
        select(Goal).where(
            Goal.event_name == event_name,
            Goal.category == category,
            Goal.objective == src.objective,
            Goal.ra_game_id == src.ra_game_id,
            Goal.achievement_id == src.achievement_id,
            Goal.custom_text == src.custom_text,
            Goal.display_text == src.display_text,
        )
    ).first()
    if existing:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    now = datetime.utcnow()
    clone = Goal(event_name=event_name, category=category, status=GoalStatus.active,
                 auto=False, created_at=now, updated_at=now)
    for f in _COPY_FIELDS:
        setattr(clone, f, getattr(src, f))
    session.add(clone)
    session.commit()
    session.refresh(clone)
    # Copy any uploaded card image to the clone's OWN file, so editing/clearing the
    # source's image later can't break the copy (both would otherwise share goal_{src}.*).
    if src.custom_image and app_settings.get(session, "covers_dir_readonly", "false") != "true":
        covers_dir = Path(app_settings.get(session, "covers_dir", "static/covers"))
        src_file = covers_dir / Path(src.custom_image).name
        if src_file.exists():
            dest = covers_dir / f"goal_{clone.id}{src_file.suffix}"
            try:
                covers_dir.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src_file.read_bytes())
                clone.custom_image = f"covers/{dest.name}"
                session.add(clone)
                session.commit()
            except OSError:
                pass
    applog.log_action("copy_goal", {"src": goal_id, "new": clone.id,
                                    "event": event_name, "category": category})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/{goal_id}/image", response_class=HTMLResponse)
async def upload_goal_image(
    request: Request,
    goal_id: int,
    image: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """Upload a custom card image for a goal — overrides the cover/badge. Saved under the
    covers dir as goal_{id}.{ext}; refused if covers are read-only."""
    goal = session.get(Goal, goal_id)
    if not goal:
        return HTMLResponse("")
    ext = Path(image.filename or "").suffix.lower()
    if ext not in _ALLOWED_IMG_EXTS:
        return _render_card(request, goal, session)
    if app_settings.get(session, "covers_dir_readonly", "false") == "true":
        return _render_card(request, goal, session)
    covers_dir = Path(app_settings.get(session, "covers_dir", "static/covers"))
    covers_dir.mkdir(parents=True, exist_ok=True)
    # Clear any prior custom image (extension may differ) before writing the new one.
    for old in covers_dir.glob(f"goal_{goal_id}.*"):
        try:
            old.unlink()
        except OSError:
            pass
    dest = covers_dir / f"goal_{goal_id}{ext}"
    dest.write_bytes(await image.read())
    goal.custom_image = f"covers/{dest.name}"
    goal.updated_at = datetime.utcnow()
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("goal_image_upload", {"id": goal_id, "file": dest.name})
    return _render_card(request, goal, session)


@router.post("/{goal_id}/image/clear", response_class=HTMLResponse)
async def clear_goal_image(request: Request, goal_id: int, session: Session = Depends(get_session)):
    """Remove a goal's uploaded custom image (falls back to text+icon / cover / letter)."""
    goal = session.get(Goal, goal_id)
    if not goal:
        return HTMLResponse("")
    covers_dir = Path(app_settings.get(session, "covers_dir", "static/covers"))
    for old in covers_dir.glob(f"goal_{goal_id}.*"):
        try:
            old.unlink()
        except OSError:
            pass
    goal.custom_image = ""
    goal.updated_at = datetime.utcnow()
    session.add(goal)
    session.commit()
    session.refresh(goal)
    return _render_card(request, goal, session)


@router.delete("/{goal_id}", response_class=HTMLResponse)
async def delete_goal(goal_id: int, session: Session = Depends(get_session)):
    goal = session.get(Goal, goal_id)
    if goal:
        applog.log_action("delete_goal", {"id": goal_id, "game": goal.game_title})
        session.delete(goal)
        session.commit()
    return HTMLResponse("")


def _render_card(request: Request, goal: Goal, session: Session) -> HTMLResponse:
    progress_by_id = {}
    if goal.ra_game_id:
        row = session.exec(
            select(RAGameProgress).where(RAGameProgress.game_id == goal.ra_game_id)
        ).first()
        if row:
            progress_by_id[goal.ra_game_id] = row
    return templates.TemplateResponse(
        request, "partials/goal_card.html",
        _card_ctx(goal, progress_by_id, datetime.utcnow(),
                  _category_names_for_event(session, goal.event_name or "")),
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _refresh_goal_art() -> None:
    """Re-pull achievement badges (one GetGameExtended per distinct game, rate-limited)
    and re-fetch game box art. Deduped by game so a big goal list ≠ a request flood."""
    from sqlmodel import Session as SyncSession
    from app.services import activity as activity_store

    with SyncSession(engine) as s:
        username = app_settings.get(s, "ra_username")
        api_key = app_settings.get(s, "ra_api_key")
        ach_games = sorted({g.ra_game_id for g in s.exec(
            select(Goal).where(Goal.objective == GoalObjective.achievement, Goal.ra_game_id != None)  # noqa: E711
        ).all()})
        cover_jobs = {}
        for g in s.exec(select(Goal).where(
            Goal.objective.in_([GoalObjective.master, GoalObjective.beaten]),
            Goal.ra_game_id != None,  # noqa: E711
        )).all():
            cover_jobs.setdefault(g.ra_game_id, (g.id, g.game_title, g.system))

    task_id = "goal-art-refresh"
    activity_store.start_batch(task_id, "Refreshing goal art", len(ach_games) + len(cover_jobs), task_type="cover")

    if username and api_key:
        ra = RAClient(username, api_key)
        for gid in ach_games:
            try:
                achievements = {a["id"]: a for a in await ra.get_achievements(gid)}  # rate-limited
            except Exception as exc:
                applog.warning("system", f"Art refresh failed for game {gid}: {exc}")
                activity_store.increment(task_id)
                continue
            with SyncSession(engine) as s:
                for g in s.exec(select(Goal).where(
                    Goal.objective == GoalObjective.achievement, Goal.ra_game_id == gid
                )).all():
                    a = achievements.get(g.achievement_id)
                    if a:
                        g.cover_path = a["badge_url"]
                        g.custom_text = a["title"] or g.custom_text
                        g.achievement_desc = a["description"]
                        g.points = a.get("points", 0) or 0
                        g.updated_at = datetime.utcnow()
                        s.add(g)
                s.commit()
            activity_store.increment(task_id)

    # Box art for master/beat goals — one cover fetch per distinct game.
    for gid, (goal_id, title, system) in cover_jobs.items():
        await _fetch_cover_goal(goal_id, gid, title, system)
        activity_store.increment(task_id)

    activity_store.finish(task_id)

    # Resolve real source game · console for each event's achievement goals (V2).
    # Backfills goals imported before source-game enrichment existed.
    for gid in ach_games:
        await events_service.enrich_source_games(gid)


async def _enrich_achievement_goal(goal_id: int, game_id: int, achievement_id: int) -> None:
    """Fill an achievement goal's title, description, and badge image from the RA
    API (API_GetGameExtended). The badge URL is stored as an absolute cover_path —
    the goal card uses it directly (no download; an HTTP page may load an HTTPS img)."""
    from sqlmodel import Session as SyncSession

    with SyncSession(engine) as s:
        username = app_settings.get(s, "ra_username")
        api_key = app_settings.get(s, "ra_api_key")
    if not (username and api_key):
        return
    try:
        achievements = await RAClient(username, api_key).get_achievements(game_id)
    except Exception as exc:
        applog.warning("system", f"Achievement goal enrich failed (game {game_id}): {exc}")
        return
    match = next((a for a in achievements if a["id"] == achievement_id), None)
    if not match:
        return
    # Best-effort RA V2: the achievement's real SOURCE game + console (an event
    # achievement is sourced from a normal game, e.g. "from Metal Arms (GC)").
    src = await events_service.fetch_source_game(api_key, achievement_id)

    with SyncSession(engine) as s:
        goal = s.get(Goal, goal_id)
        if goal:
            if match["title"]:
                goal.custom_text = match["title"]
            goal.achievement_desc = match["description"]
            goal.points = match.get("points", 0) or 0
            if match["badge_url"]:
                goal.cover_path = match["badge_url"]   # absolute URL
            if src and src.get("title"):
                goal.game_title = src["title"]
                if src.get("console"):
                    goal.system = src["console"]
            goal.updated_at = datetime.utcnow()
            s.add(goal)
            s.commit()


async def _fetch_game_cover_file(ra_game_id: int, game_title: str, system: str) -> bool:
    """Fetch box art for a game (first enabled cover source wins) into the shared
    covers/{ra_game_id}.png filename. Returns True if a cover was written. No activity
    tracking or DB stamping — the callers (_fetch_cover_goal / _enrich_category) own those."""
    import json as _json
    from sqlmodel import Session as SyncSession
    from app.db.models import AppSetting
    from app.services import cover_sources as cover_source_registry

    with SyncSession(engine) as s:
        def _gs(key: str, default: str = "") -> str:
            setting = s.get(AppSetting, key)
            return setting.value if setting else default

        covers_dir = Path(_gs("covers_dir", "static/covers"))
        if _gs("covers_dir_readonly", "false") == "true":
            return False

        config: dict = {"ra_username": _gs("ra_username"), "ra_api_key": _gs("ra_api_key")}
        for src in cover_source_registry.all_sources():
            if src.requires_api_key:
                k = f"cover_source_{src.source_id}_api_key"
                config[k] = _gs(k)

        all_srcs = cover_source_registry.all_sources()
        order_raw = _gs("cover_sources_order", "")
        if order_raw:
            try:
                order = _json.loads(order_raw)
                src_map = {x.source_id: x for x in all_srcs}
                ordered = [src_map[sid] for sid in order if sid in src_map]
                ordered_ids = {x.source_id for x in ordered}
                ordered += [x for x in all_srcs if x.source_id not in ordered_ids]
            except (ValueError, KeyError):
                ordered = all_srcs
        else:
            ordered = all_srcs
        enabled_srcs = [x for x in ordered if _gs(f"cover_source_{x.source_id}_enabled", "false") == "true"]

    image_bytes: bytes | None = None
    for src in enabled_srcs:
        try:
            image_bytes = await src.fetch_cover(ra_game_id, game_title, system, config)
            if image_bytes:
                break
        except Exception:
            continue

    if image_bytes:
        covers_dir.mkdir(parents=True, exist_ok=True)
        (covers_dir / f"{ra_game_id}.png").write_bytes(image_bytes)
        return True
    return False


async def _fetch_cover_goal(goal_id: int, ra_game_id: int, game_title: str, system: str) -> None:
    """Fetch cover art for a goal's game (first enabled source wins), reusing the
    shared covers/{ra_game_id}.png filename. Mirrors wanted._fetch_cover."""
    from sqlmodel import Session as SyncSession
    from app.services import activity as activity_store

    task_id = f"cover-goal-{goal_id}"
    activity_store.start(task_id, f"Cover art: {game_title}", task_type="cover")
    try:
        if await _fetch_game_cover_file(ra_game_id, game_title, system):
            with SyncSession(engine) as session:
                goal = session.get(Goal, goal_id)
                if goal:
                    goal.cover_path = f"covers/{ra_game_id}.png"
                    goal.updated_at = datetime.utcnow()
                    session.add(goal)
                    session.commit()
    finally:
        activity_store.finish(task_id)


async def _enrich_category(category_id: int, ra_game_id: int, need_name: bool) -> None:
    """Fill a sub-category's attached-game identity from RA: its title (only when no title
    was typed — `need_name`), console, and box art. The box art reuses any cover already on
    disk for this game; otherwise it's fetched via the cover sources. Renaming the category
    re-points its goals (Goal.category is the join key)."""
    from sqlmodel import Session as SyncSession
    from app.services import activity as activity_store

    # Resolve title (only when none was typed) + console from RA. The console is filled
    # whenever the category doesn't already carry one (e.g. the by-id path with a typed title).
    with SyncSession(engine) as s:
        username = app_settings.get(s, "ra_username")
        api_key = app_settings.get(s, "ra_api_key")
        cat0 = s.get(GoalCategory, category_id)
        need_system = bool(cat0 and not cat0.system)
    title = system = ""
    if (need_name or need_system) and username and api_key:
        try:
            info = await RAClient(username, api_key).get_game_info(ra_game_id)
            title = (info.get("Title") or "").strip()
            system = (info.get("ConsoleName") or "").strip()
        except Exception as exc:
            applog.warning("system", f"Category game enrich failed (game {ra_game_id}): {exc}")

    # Box art: reuse an on-disk cover if present, else fetch one.
    task_id = f"cover-cat-{category_id}"
    activity_store.start(task_id, f"Category art: {title or ra_game_id}", task_type="cover")
    have_cover = False
    try:
        with SyncSession(engine) as s:
            covers_dir = Path(app_settings.get(s, "covers_dir", "static/covers"))
            existing = s.get(GoalCategory, category_id)
            cat_title = title or (existing.name if existing else "")
        if (covers_dir / f"{ra_game_id}.png").exists():
            have_cover = True
        elif await _fetch_game_cover_file(ra_game_id, cat_title, system):
            have_cover = True
    finally:
        activity_store.finish(task_id)

    with SyncSession(engine) as s:
        cat = s.get(GoalCategory, category_id)
        if not cat:
            return
        old_name = cat.name
        if need_name and title:
            cat.name = title
        if system:
            cat.system = system
        if have_cover:
            cat.cover_path = f"covers/{ra_game_id}.png"
        cat.updated_at = datetime.utcnow()
        s.add(cat)
        # Re-point goals if the placeholder name was replaced with the real game title.
        if need_name and title and title != old_name:
            for g in s.exec(select(Goal).where(
                    Goal.event_name == cat.event_name, Goal.category == old_name)).all():
                g.category = title
                s.add(g)
        s.commit()
