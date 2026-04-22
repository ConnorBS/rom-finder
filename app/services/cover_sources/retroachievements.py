import httpx

from .base import CoverSource

_RA_API = "https://retroachievements.org/API"
_RA_MEDIA = "https://media.retroachievements.org"


class RetroAchievementsCoverSource(CoverSource):
    source_id = "retroachievements"
    name = "RetroAchievements"
    description = "Game icon from the RetroAchievements media CDN. Uses the RA credentials configured above."
    requires_api_key = False

    async def fetch_cover(self, ra_game_id: int, game_title: str, system: str, config: dict) -> bytes | None:
        username = config.get("ra_username", "")
        api_key = config.get("ra_api_key", "")
        if not username or not api_key:
            return None
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(
                    f"{_RA_API}/API_GetGame.php",
                    params={"z": username, "y": api_key, "i": ra_game_id},
                )
                resp.raise_for_status()
                icon = resp.json().get("ImageIcon", "")
                if not icon:
                    return None
                img = await client.get(f"{_RA_MEDIA}{icon}")
                if img.status_code == 200:
                    return img.content
        except Exception:
            pass
        return None
