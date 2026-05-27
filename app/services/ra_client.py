"""RetroAchievements API client.

API docs: https://api.docs.retroachievements.org/
Requires a free account and API key from retroachievements.org/settings
"""

import asyncio
import logging
import re
import time

import httpx
from typing import Optional

from app.services.sources.errors import SourceRateLimitError

logger = logging.getLogger(__name__)

RA_BASE_URL = "https://retroachievements.org/API"

# ---------------------------------------------------------------------------
# Rate limiter — shared across all RAClient instances.
# RA's documented limit is 500 req/min (~8.3/s). We target 2/s (120/min)
# to stay comfortably clear. On a 429 we back off using Retry-After.
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, calls_per_second: float = 2.0):
        self._interval = 1.0 / calls_per_second
        self._lock = asyncio.Lock()
        self._last: float = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            gap = self._interval - (now - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()

_limiter = _RateLimiter()  # 2 req/sec = 120/min


def _normalize_title(s: str) -> str:
    """Normalize a game title for fuzzy substring matching.
    RA stores titles as 'Game - Subtitle' but users type 'Game: Subtitle'.
    Replaces all punctuation with spaces and collapses whitespace."""
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()

# Maps RA system name -> folder name on disk.
# Only entries where the folder name differs from the system name are needed;
# _resolve_folder falls back to the system name itself when neither this map
# nor the user's custom folder_map has an entry.
DEFAULT_FOLDER_MAP: dict[str, str] = {
    "NES":                         "Nintendo Entertainment System",
    "SNES":                        "Super Nintendo Entertainment System",
    "Game Boy":                    "Nintendo Game Boy",
    "Game Boy Advance":            "Nintendo Game Boy Advanced",
    "Game Boy Color":              "Nintendo Game Boy Color",
    "GameCube":                    "Nintendo Gamecube",
    "Wii":                         "Wii",
    "PlayStation":                 "Sony Playstation",
    "PlayStation 2":               "Sony Playstation 2",
    "PlayStation Portable":        "Playstation Portable",
    "Sega Genesis / Mega Drive":   "Sega Genesis",
    "Dreamcast":                   "Sega Dreamcast",
    "Master System":               "Sega Master System",
    "Saturn":                      "Sega Saturn",
    "Game Gear":                   "gamegear",
    "Atari 2600":                  "atari2600",
    "Atari 7800":                  "atari7800",
    "Atari Jaguar":                "jaguar",
    "PC Engine / TurboGrafx-16":  "turbografx-16",
    "PC Engine CD":                "turbografx-cd",
    "MSX":                         "Microsoft - MSX",
    "Neo Geo Pocket":              "SNK Neo Geo Pocket",
    "Pokemon Mini":                "pokemon-mini",
    "3DO Interactive Multiplayer": "3DO",
}

# System ID -> display name mapping
# IDs match RetroAchievements console IDs from API_GetConsoleIDs.php
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
    19: "Wii",
    20: "Wii U",
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
    63: "Watara Supervision",
    69: "Mega Duck",
    71: "Arduboy",
    72: "WASM-4",
    76: "PC Engine CD",
    78: "Nintendo DSi",
    80: "GameCube",
    89: "Uzebox",
}

# Platforms RetroAchievements has no console/hashing support for — ROMs here can
# NEVER hash-match, so the UI shows them as "platform not supported" rather than
# "no RA match" failures, and the resumable verify skips them (no wasted RA calls).
# Curated, NOT derived from `SYSTEMS`: misnamed-but-supported folders (e.g. "tg16",
# "mega-duck-slash-cougar-boy") do verify, so excluding everything absent from
# SYSTEMS would wrongly hide real matches. Add entries here as such platforms appear.
RA_UNSUPPORTED_SYSTEMS: set[str] = {
    "Nintendo 3DS",
    "Archipelago",
}


def is_ra_unsupported(system: str) -> bool:
    return system in RA_UNSUPPORTED_SYSTEMS


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
        await _limiter.wait()
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
        await _limiter.wait()
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
        await _limiter.wait()
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
        """Search for games on a system by title (case-insensitive substring match).
        Fetches all games so games without verified dumps still appear.
        Normalizes punctuation — RA titles use ' - ' where users often type ': '."""
        await _limiter.wait()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{RA_BASE_URL}/API_GetGameList.php",
                params=self._params({"i": system_id}),
                timeout=30,
            )
            resp.raise_for_status()
            games = resp.json()
        q = _normalize_title(query)
        return [g for g in games if q in _normalize_title(g.get("Title", ""))]

    async def lookup_hash(self, md5: str) -> Optional[dict]:
        """Look up a game by its ROM MD5 hash. Returns game info dict or None.

        Uses the emulator-style dorequest endpoint (API_GetGameInfoByMD5.php
        was found to return 404 for all hashes regardless of database state).
        Falls back to API_GetGameInfoByMD5.php if dorequest is blocked.
        On 429 waits for Retry-After then retries once before giving up.
        """
        await _limiter.wait()
        headers = {"User-Agent": "RetroAchievements/1.0"}
        async with httpx.AsyncClient(headers=headers) as client:
            # Primary: emulator-style endpoint, returns {"Success":true,"GameID":N}
            resp = await client.get(
                "https://retroachievements.org/dorequest.php",
                params={"r": "gameid", "u": self.username, "m": md5},
                timeout=15,
            )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning("RA rate limit hit (429); waiting %ds before retry", retry_after)
                await asyncio.sleep(retry_after)
                await _limiter.wait()
                resp = await client.get(
                    "https://retroachievements.org/dorequest.php",
                    params={"r": "gameid", "u": self.username, "m": md5},
                    timeout=15,
                )
                if resp.status_code == 429:
                    raise SourceRateLimitError(
                        "RA rate limit persists after retry — skipping hash",
                        retry_after=float(resp.headers.get("Retry-After", retry_after)),
                    )

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("Success") and data.get("GameID"):
                        data["ID"] = data["GameID"]
                        return data
                    # GameID=0 means not found — genuine miss, don't fall back
                    if isinstance(data, dict) and data.get("Success"):
                        return None
                except Exception:
                    pass  # fall through to legacy endpoint

            # Fallback: legacy endpoint (returns 404 for most hashes but may work in some cases)
            logger.debug("dorequest returned %d for %s, trying legacy endpoint", resp.status_code, md5)
            await _limiter.wait()
            resp2 = await client.get(
                f"{RA_BASE_URL}/API_GetGameInfoByMD5.php",
                params=self._params({"m": md5}),
                timeout=15,
            )
            if resp2.status_code == 429:
                retry_after = int(resp2.headers.get("Retry-After", 60))
                logger.warning("RA rate limit hit (429) on fallback; waiting %ds", retry_after)
                raise SourceRateLimitError(
                    "RA rate limit on fallback — skipping hash", retry_after=float(retry_after),
                )
            if resp2.status_code == 404:
                return None
            resp2.raise_for_status()
            try:
                data2 = resp2.json()
            except Exception:
                return None

        if not isinstance(data2, dict):
            return None
        payload = data2.get("GameData") if isinstance(data2.get("GameData"), dict) else data2
        game_id = payload.get("ID") or payload.get("GameID")
        if not game_id:
            return None
        payload["ID"] = game_id
        return payload

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
        await _limiter.wait()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{RA_BASE_URL}/API_GetGame.php",
                params=self._params({"i": game_id}),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
