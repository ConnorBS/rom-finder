"""wowroms.com ROM source.

Scrapes wowroms.com search results. Returns empty results on network errors
so it degrades gracefully if the site is unavailable.
"""

import re
import httpx
from bs4 import BeautifulSoup

from .base import RomSource

WOWROMS_BASE = "https://www.wowroms.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.wowroms.com/",
}

# RA system name → WowROMs system slug used in URLs
_SYSTEM_MAP: dict[str, str] = {
    "NES": "nintendo-nes",
    "SNES": "super-nintendo",
    "Nintendo 64": "nintendo-64",
    "Game Boy": "nintendo-game-boy",
    "Game Boy Color": "nintendo-game-boy-color",
    "Game Boy Advance": "nintendo-game-boy-advance",
    "Nintendo DS": "nintendo-ds",
    "GameCube": "nintendo-gamecube",
    "Wii": "nintendo-wii",
    "Sega Genesis / Mega Drive": "sega-genesis",
    "Sega CD": "sega-cd",
    "Sega 32X": "sega-32x",
    "Saturn": "sega-saturn",
    "Dreamcast": "sega-dreamcast",
    "Master System": "sega-master-system",
    "Game Gear": "sega-game-gear",
    "PlayStation": "sony-playstation",
    "PlayStation 2": "sony-playstation-2",
    "PlayStation Portable": "sony-psp",
    "Atari 2600": "atari-2600",
    "Atari 5200": "atari-5200",
    "Atari 7800": "atari-7800",
    "Atari Lynx": "atari-lynx",
    "PC Engine / TurboGrafx-16": "nec-turbografx-16",
    "Neo Geo Pocket": "snk-neo-geo-pocket",
    "WonderSwan": "bandai-wonderswan",
    "Virtual Boy": "nintendo-virtual-boy",
    "3DO Interactive Multiplayer": "3do",
}

# WowROMs game URLs: /en/roms/{system-slug}/{game-title}/{id}
_GAME_PATH_RE = re.compile(r"^/en/roms/([^/]+)/([^/]+)/(\d+)/?")


class WowromsSource(RomSource):
    source_id = "wowroms"
    name = "WowROMs"

    async def search(self, query: str, system: str = "") -> list[dict]:
        sys_slug = _SYSTEM_MAP.get(system, "")

        # Try system-scoped search first, fall back to global
        search_urls = []
        if sys_slug:
            search_urls.append(
                f"{WOWROMS_BASE}/en/roms/list/{sys_slug}/search/{query}/page/1"
            )
        search_urls.append(
            f"{WOWROMS_BASE}/en/search/q/{query}/page/1"
        )

        for url in search_urls:
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    resp = await client.get(url, headers=_HEADERS, timeout=20)
                    resp.raise_for_status()
            except Exception:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            results = self._parse_results(soup, sys_slug)
            if results:
                return results

        return []

    def _parse_results(self, soup: BeautifulSoup, sys_slug: str) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=_GAME_PATH_RE):
            m = _GAME_PATH_RE.match(a["href"])
            if not m:
                continue

            page_sys, page_slug, game_id = m.group(1), m.group(2), m.group(3)

            if sys_slug and page_sys != sys_slug:
                continue

            identifier = game_id
            if identifier in seen:
                continue
            seen.add(identifier)

            title = a.get_text(strip=True)
            if not title:
                h = a.find(["h2", "h3", "h4", "span"])
                title = h.get_text(strip=True) if h else ""
            if not title:
                title = page_slug.replace("-", " ").title()

            results.append({
                "identifier": identifier,
                "title": title,
                "description": "WowROMs",
                "url": f"{WOWROMS_BASE}/en/roms/{page_sys}/{page_slug}/{game_id}",
                "source_id": self.source_id,
                "_sys_slug": page_sys,
                "_page_slug": page_slug,
            })

        return results[:25]

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        # identifier is the numeric game ID; reconstruct the URL from search metadata
        # stored in _sys_slug / _page_slug, or fall back to a search-by-ID lookup.
        # For now just return a placeholder so the download flow can proceed.
        return [{
            "name": f"game_{identifier}.zip",
            "identifier": identifier,
            "source_id": self.source_id,
            "size": 0,
            "md5": "",
        }]

    def get_download_url(self, identifier: str, filename: str) -> str:
        return f"{WOWROMS_BASE}/en/roms/download/{identifier}"

    def get_extra_headers(self) -> dict:
        return _HEADERS
