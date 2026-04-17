"""RetroAchievements API client.

API docs: https://api.docs.retroachievements.org/
Requires a free account and API key from retroachievements.org/settings
"""

import httpx
from typing import Optional

RA_BASE_URL = "https://retroachievements.org/API"

# System ID -> display name mapping
SYSTEMS: dict[int, str] = {
    1: "Sega Genesis / Mega Drive",
    2: "Nintendo 64",
    3: "SNES",
    4: "Game Boy",
    5: "Game Boy Advance",
    6: "Game Boy Color",
    7: "NES",
    8: "PC Engine / TurboGrafx-16",
    9: "Sega CD",
    10: "Sega 32X",
    11: "Master System",
    12: "PlayStation",
    13: "Atari Lynx",
    14: "Neo Geo Pocket",
    15: "Game Gear",
    17: "Atari Jaguar",
    18: "Nintendo DS",
    21: "PlayStation 2",
    23: "Magnavox Odyssey 2",
    24: "Pokemon Mini",
    25: "Atari 2600",
    27: "Arcade",
    28: "Virtual Boy",
    29: "MSX",
    33: "SG-1000",
    37: "Amstrad CPC",
    38: "Apple II",
    39: "Saturn",
    40: "Dreamcast",
    41: "PlayStation Portable",
    43: "3DO Interactive Multiplayer",
    44: "ColecoVision",
    45: "Intellivision",
    46: "Vectrex",
    47: "PC-8000/8800",
    49: "PC-FX",
    51: "Atari 7800",
    53: "WonderSwan",
    56: "Fairchild Channel F",
    57: "Philips CD-i",
    76: "PC Engine CD",
    78: "Nintendo DSi",
    80: "GameCube",
}


class RAClient:
    def __init__(self, username: str, api_key: str):
        self.username = username
        self.api_key = api_key

    def _params(self, extra: dict | None = None) -> dict:
        params = {"z": self.username, "y": self.api_key}
        if extra:
            params.update(extra)
        return params

    async def get_game_list(self, system_id: int) -> list[dict]:
        """Fetch all games for a given system, including hash count."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{RA_BASE_URL}/API_GetGameList.php",
                params=self._params({"i": system_id, "h": 1}),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_game_hashes(self, game_id: int) -> list[str]:
        """Return the list of accepted MD5 hashes for a game."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{RA_BASE_URL}/API_GetGameHashes.php",
                params=self._params({"i": game_id}),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return [h["MD5"] for h in data.get("Results", [])]

    async def get_game_hashes_full(self, game_id: int) -> list[dict]:
        """Return full hash entries (MD5, Name, Labels) for a game."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{RA_BASE_URL}/API_GetGameHashes.php",
                params=self._params({"i": game_id}),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("Results", [])

    async def search_games(self, system_id: int, query: str) -> list[dict]:
        """Search for games on a system by title (case-insensitive substring match)."""
        games = await self.get_game_list(system_id)
        q = query.lower()
        return [g for g in games if q in g.get("Title", "").lower()]

    async def lookup_hash(self, md5: str) -> Optional[dict]:
        """Look up a game by its ROM MD5 hash. Returns game info or None."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{RA_BASE_URL}/API_GetGameInfoByMD5.php",
                params=self._params({"m": md5}),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if data.get("ID") else None

    async def test_credentials(self) -> tuple[bool, str]:
        """Test if credentials are valid. Returns (success, message)."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{RA_BASE_URL}/API_GetUserProfile.php",
                    params={"z": self.username, "y": self.api_key, "u": self.username},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("User"):
                    return True, f"Connected as {data['User']}"
                error = data.get("Error", "Invalid credentials or no response")
                return False, error
        except httpx.HTTPStatusError as e:
            return False, f"HTTP {e.response.status_code}"
        except Exception as e:
            return False, str(e)

    async def get_game_info(self, game_id: int) -> dict:
        """Fetch detailed info for a single game including achievement count."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{RA_BASE_URL}/API_GetGame.php",
                params=self._params({"i": game_id}),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
