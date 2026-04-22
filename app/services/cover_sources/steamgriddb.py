from urllib.parse import quote

import httpx

from .base import CoverSource

_SGDB_BASE = "https://www.steamgriddb.com/api/v2"


class SteamGridDBCoverSource(CoverSource):
    source_id = "steamgriddb"
    name = "SteamGridDB"
    description = "High-quality box art and cover images. Requires a free API key from steamgriddb.com/profile/api."
    requires_api_key = True

    async def fetch_cover(self, ra_game_id: int, game_title: str, system: str, config: dict) -> bytes | None:
        api_key = config.get("cover_source_steamgriddb_api_key", "")
        if not api_key:
            return None
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                search = await client.get(
                    f"{_SGDB_BASE}/search/autocomplete/{quote(game_title)}",
                    headers=headers,
                )
                search.raise_for_status()
                games = search.json().get("data", [])
                if not games:
                    return None

                game_id = games[0]["id"]
                grids = await client.get(
                    f"{_SGDB_BASE}/grids/game/{game_id}",
                    headers=headers,
                    params={"types": "static"},
                )
                grids.raise_for_status()
                items = grids.json().get("data", [])
                if not items:
                    return None

                img_url = items[0].get("url", "")
                if not img_url:
                    return None
                img = await client.get(img_url)
                if img.status_code == 200:
                    return img.content
        except Exception:
            pass
        return None
