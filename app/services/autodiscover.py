"""Autodiscover: check RA for newly-added games and add them to the Wanted pool."""
from datetime import datetime, timedelta
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import AppSetting, WantedGame, LibraryEntry
from app.services.ra_client import RAClient, SYSTEMS
from app.services import logger as applog


def _get_setting(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


async def run_autodiscover() -> dict:
    """Run one autodiscover pass. Returns a summary dict with 'added' and 'systems_checked'."""
    from app.services import activity as activity_store

    with Session(engine) as session:
        username = _get_setting(session, "ra_username")
        api_key = _get_setting(session, "ra_api_key")
        last_checked_str = _get_setting(session, "ra_autodiscover_last_checked", "")

    if not username or not api_key:
        return {"error": "No RA credentials configured", "added": 0, "systems_checked": 0}

    if last_checked_str:
        try:
            last_checked = datetime.fromisoformat(last_checked_str)
        except ValueError:
            last_checked = datetime.utcnow() - timedelta(hours=24)
    else:
        # First run: look back 24 h so genuinely new games surface immediately
        last_checked = datetime.utcnow() - timedelta(hours=24)

    ra = RAClient(username, api_key)

    with Session(engine) as session:
        wanted_systems = {w.system for w in session.exec(select(WantedGame)).all() if w.system}
        library_systems = {e.system for e in session.exec(select(LibraryEntry)).all() if e.system}
        tracked_systems = wanted_systems | library_systems

        existing_ra_ids: set[int] = set()
        existing_ra_ids.update(w.ra_game_id for w in session.exec(select(WantedGame)).all() if w.ra_game_id)
        existing_ra_ids.update(e.ra_game_id for e in session.exec(select(LibraryEntry)).all() if e.ra_game_id)

    system_name_to_id = {v: k for k, v in SYSTEMS.items()}
    system_ids_to_check = [
        (system_name_to_id[sys], sys)
        for sys in tracked_systems
        if sys in system_name_to_id
    ]

    _update_last_checked()

    if not system_ids_to_check:
        applog.info("autodiscover", "No tracked systems to check")
        return {"added": 0, "systems_checked": 0}

    activity_store.start(
        "autodiscover",
        f"Autodiscover ({len(system_ids_to_check)} system{'s' if len(system_ids_to_check) != 1 else ''})",
        task_type="task",
    )

    added = 0
    errors = 0

    for sys_id, sys_name in system_ids_to_check:
        try:
            games = await ra.get_game_list(sys_id)
            new_games: list[tuple[int, str]] = []
            for game in games:
                ra_id = game.get("ID")
                title = game.get("Title", "")
                if not ra_id or not title:
                    continue
                if (game.get("NumAchievements") or 0) == 0:
                    continue
                if ra_id in existing_ra_ids:
                    continue
                date_str = game.get("DateModified", "")
                if not date_str:
                    continue
                try:
                    date_modified = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if date_modified <= last_checked:
                    continue
                new_games.append((ra_id, title))

            if new_games:
                with Session(engine) as session:
                    for ra_id, title in new_games:
                        if ra_id not in existing_ra_ids:
                            session.add(WantedGame(game_title=title, system=sys_name, ra_game_id=ra_id))
                            existing_ra_ids.add(ra_id)
                            added += 1
                    session.commit()

        except Exception as exc:
            applog.warning("autodiscover", f"Failed checking {sys_name}: {exc}")
            errors += 1

    activity_store.finish("autodiscover")
    applog.info("autodiscover", f"Complete — {added} added, {errors} errors, {len(system_ids_to_check)} systems")
    return {"added": added, "errors": errors, "systems_checked": len(system_ids_to_check)}


def _update_last_checked() -> None:
    with Session(engine) as session:
        s = session.get(AppSetting, "ra_autodiscover_last_checked") or AppSetting(key="ra_autodiscover_last_checked")
        s.value = datetime.utcnow().isoformat()
        session.add(s)
        session.commit()
