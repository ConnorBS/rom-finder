from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from pathlib import Path
from datetime import datetime

from app.db.database import get_session, engine
from app.db.models import Goal, GoalObjective, GoalStatus, RAGameProgress
from app.services import settings as app_settings
from app.services import logger as applog
from app.services.goals import evaluate_goals
from app.services.ra_client import SYSTEMS, RAClient

router = APIRouter(prefix="/goals")
templates = Jinja2Templates(directory="app/templates")


def _ra_configured(session: Session) -> bool:
    return bool(app_settings.get(session, "ra_username") and app_settings.get(session, "ra_api_key"))


def _parse_deadline(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _card_ctx(goal: Goal, progress_by_id: dict, now: datetime) -> dict:
    """Per-goal render context: the matched RA progress row + deadline state."""
    progress = progress_by_id.get(goal.ra_game_id) if goal.ra_game_id else None
    overdue = bool(goal.status == GoalStatus.active and goal.deadline and goal.deadline < now)
    days_left = (goal.deadline.date() - now.date()).days if goal.deadline else None
    return {"goal": goal, "progress": progress, "overdue": overdue, "days_left": days_left, "now": now}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def goals_page(request: Request, session: Session = Depends(get_session)):
    evaluate_goals(session)  # LOCAL — fold in any RA progress since last visit

    goals = session.exec(select(Goal).order_by(Goal.created_at.desc())).all()
    progress_by_id = {r.game_id: r for r in session.exec(select(RAGameProgress)).all()}
    now = datetime.utcnow()

    # Group by event ("" = ungrouped, rendered last). Preserve first-seen order
    # of event names so the layout is stable between visits.
    grouped: dict[str, list] = {}
    for g in goals:
        grouped.setdefault(g.event_name or "", []).append(_card_ctx(g, progress_by_id, now))
    ordered_events = [e for e in grouped if e] + ([""] if "" in grouped else [])
    groups = [
        {
            "name": e,
            "cards": grouped[e],
            "done": sum(1 for c in grouped[e] if c["goal"].status == GoalStatus.completed),
            "total": len(grouped[e]),
        }
        for e in ordered_events
    ]
    event_names = sorted({g.event_name for g in goals if g.event_name})

    applog.log_navigation("goals", {"goal_count": len(goals)})
    return templates.TemplateResponse(
        request, "goals.html",
        {
            "groups": groups,
            "count": len(goals),
            "event_names": event_names,
            "systems": sorted(SYSTEMS.items(), key=lambda x: x[1]),
            "ra_configured": _ra_configured(session),
            "now": now,
        },
    )


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


@router.post("/{goal_id}/edit", response_class=HTMLResponse)
async def edit_goal(
    request: Request,
    goal_id: int,
    event_name: str = Form(default=""),
    deadline: str = Form(default=""),
    custom_text: str = Form(default=""),
    session: Session = Depends(get_session),
):
    goal = session.get(Goal, goal_id)
    if not goal:
        return HTMLResponse("")
    goal.event_name = event_name.strip()
    goal.deadline = _parse_deadline(deadline)
    if goal.objective == GoalObjective.custom and custom_text.strip():
        goal.custom_text = custom_text.strip()
    goal.updated_at = datetime.utcnow()
    session.add(goal)
    session.commit()
    session.refresh(goal)
    applog.log_action("edit_goal", {"id": goal_id, "event": goal.event_name})
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
        _card_ctx(goal, progress_by_id, datetime.utcnow()),
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

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
    with SyncSession(engine) as s:
        goal = s.get(Goal, goal_id)
        if goal:
            if match["title"]:
                goal.custom_text = match["title"]
            goal.achievement_desc = match["description"]
            if match["badge_url"]:
                goal.cover_path = match["badge_url"]   # absolute URL
            goal.updated_at = datetime.utcnow()
            s.add(goal)
            s.commit()


async def _fetch_cover_goal(goal_id: int, ra_game_id: int, game_title: str, system: str) -> None:
    """Fetch cover art for a goal's game (first enabled source wins), reusing the
    shared covers/{ra_game_id}.png filename. Mirrors wanted._fetch_cover."""
    import json as _json
    from sqlmodel import Session as SyncSession
    from app.db.models import AppSetting
    from app.services import cover_sources as cover_source_registry
    from app.services import activity as activity_store

    task_id = f"cover-goal-{goal_id}"
    activity_store.start(task_id, f"Cover art: {game_title}", task_type="cover")

    with SyncSession(engine) as s:
        def _gs(key: str, default: str = "") -> str:
            setting = s.get(AppSetting, key)
            return setting.value if setting else default

        covers_dir = Path(_gs("covers_dir", "static/covers"))
        if _gs("covers_dir_readonly", "false") == "true":
            activity_store.finish(task_id)
            return

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

    covers_dir.mkdir(parents=True, exist_ok=True)

    image_bytes: bytes | None = None
    for src in enabled_srcs:
        try:
            image_bytes = await src.fetch_cover(ra_game_id, game_title, system, config)
            if image_bytes:
                break
        except Exception:
            continue

    try:
        if image_bytes:
            (covers_dir / f"{ra_game_id}.png").write_bytes(image_bytes)
            with SyncSession(engine) as session:
                goal = session.get(Goal, goal_id)
                if goal:
                    goal.cover_path = f"covers/{ra_game_id}.png"
                    goal.updated_at = datetime.utcnow()
                    session.add(goal)
                    session.commit()
    finally:
        activity_store.finish(task_id)
