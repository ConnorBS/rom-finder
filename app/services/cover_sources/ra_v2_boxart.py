import httpx

from .base import CoverSource
from app.services.ra_client_v2 import RAClientV2


class RAV2BoxArtCoverSource(CoverSource):
    """Full box art from the RA **V2** API (`/games/{id}` → `imageBoxArtUrl`) — higher
    quality than the V1 game icon. Uses the configured RA API key (Bearer). Falls
    through (returns None) when V2 is unreachable, so the next cover source is tried."""
    source_id = "ra_v2_boxart"
    name = "RetroAchievements box art (V2)"
    description = "Full box art from the RetroAchievements V2 API. Uses the RA credentials above."
    requires_api_key = False

    async def fetch_cover(self, ra_game_id: int, game_title: str, system: str, config: dict) -> bytes | None:
        if not ra_game_id:
            return None
        api_key = config.get("ra_api_key", "")
        if not api_key:
            return None
        try:
            data = await RAClientV2(api_key).get_game(ra_game_id)
            attrs = (data.get("data") or {}).get("attributes", {}) if isinstance(data, dict) else {}
            url = attrs.get("imageBoxArtUrl") or ""
            if not url:
                return None
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                img = await client.get(url)
                if img.status_code == 200 and img.content:
                    return img.content
        except Exception:
            pass
        return None
