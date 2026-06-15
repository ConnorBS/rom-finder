"""RA V2 hub import — bulk-add every game in an RA hub (series/theme, e.g.
"Hacks - Mario", "Series - Final Fantasy") to the Wanted pool.

`fetch_hub_games` pulls the hub's full game list from the V2 API (paginated, globally
rate-limited); the /wanted/import-hub endpoint creates the WantedGame rows. Mirrors the
event-import pattern. NO DB session is held across the RA awaits here.
"""
import re

from sqlmodel import Session

from app.db.database import engine
from app.services import settings as app_settings
from app.services import logger as applog
from app.services.ra_client_v2 import RAClientV2

_MAX_PAGES = 30   # 30 * 100 = 3000 games — a flood guard for pathological hubs.

# RA returns "beaten-hardcore"/"beaten-softcore" for the beaten tiers; mirror
# services/mastery._BEATEN_KINDS so the hub-import progress filter agrees with the
# collection's Beaten/Mastered badges.
_BEATEN_KINDS = ("beaten", "beaten-hardcore", "beaten-softcore", "completed")


def progress_bucket(award: str, num_awarded: int) -> str:
    """Classify a game by the configured user's progress (from the LOCAL RA mirror) for
    the hub-import filters — zero RA calls:
      'mastered' — hardcore 100%
      'beaten'   — beaten/completed but not mastered
      'some'     — has hardcore unlocks but no award yet
      'none'     — nothing earned
    """
    award = (award or "").lower()
    if award == "mastered":
        return "mastered"
    if award in _BEATEN_KINDS:
        return "beaten"
    return "some" if num_awarded > 0 else "none"


def parse_hub_ref(text: str) -> int | None:
    """Hub id from a pasted URL or bare number (`/hub/123`, `/game/123`, `123`)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    m = re.search(r"/(?:hub|game)/(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


async def fetch_hub_games(hub_id: int) -> dict:
    """All games in a hub → {"games": [{game_id, title, console}, ...]} or {"error": ...}.
    Paginates the V2 endpoint; reads creds in a short session, then only awaits."""
    with Session(engine) as s:
        api_key = app_settings.get(s, "ra_api_key")
    if not api_key:
        return {"error": "no_credentials"}
    ra = RAClientV2(api_key)
    games: list[dict] = []
    seen: set[int] = set()
    try:
        for page in range(1, _MAX_PAGES + 1):
            data = await ra.get_hub_games(hub_id, page=page, size=100)
            batch = RAClientV2.games_from_payload(data)
            if not batch:
                break
            for g in batch:
                if g["game_id"] and g["game_id"] not in seen:
                    seen.add(g["game_id"])
                    games.append(g)
            if len(batch) < 100:
                break
    except Exception as exc:
        if games:
            applog.warning("system", f"Hub {hub_id} games: partial fetch ({exc})")
        else:
            return {"error": str(exc)}
    return {"games": games}
