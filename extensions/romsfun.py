"""ROMsFun ROM source extension for ROM Finder."""

EXTENSION_INFO = {
    "id": "romsfun",
    "name": "ROMsFun",
    "version": "1.0.0",
    "type": "rom_source",
    "author": "ConnorBS",
    "description": "Searches ROMsFun.com for ROMs. May be blocked by Cloudflare bot protection.",
}

import re
import httpx
from bs4 import BeautifulSoup

from app.services.sources.base import RomSource

ROMSFUN_BASE = "https://romsfun.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://romsfun.com/",
}

# RA system name → ROMsFun system slug (used in game page URLs)
_SYSTEM_MAP: dict[str, str] = {
    "NES": "nes",
    "SNES": "super-nintendo",
    "Nintendo 64": "nintendo-64",
    "Game Boy": "game-boy",
    "Game Boy Color": "game-boy-color",
    "Game Boy Advance": "game-boy-advance",
    "Nintendo DS": "nintendo-ds",
    "GameCube": "gamecube",
    "Wii": "wii",
    "Sega Genesis / Mega Drive": "sega-genesis",
    "Sega CD": "sega-cd",
    "Sega 32X": "sega-32x",
    "Saturn": "sega-saturn",
    "Dreamcast": "dreamcast",
    "Master System": "sega-master-system",
    "Game Gear": "sega-game-gear",
    "PlayStation": "playstation",
    "PlayStation 2": "playstation-2",
    "PlayStation Portable": "psp",
    "Atari 2600": "atari-2600",
    "Atari 5200": "atari-5200",
    "Atari 7800": "atari-7800",
    "PC Engine / TurboGrafx-16": "turbografx-16",
    "Neo Geo Pocket": "neo-geo-pocket",
    "WonderSwan": "wonderswan",
    "Virtual Boy": "virtual-boy",
    "Nintendo 3DS": "nintendo-3ds",
    "Nintendo Switch": "nintendo-switch",
}

# Game page URLs: /roms/{system-slug}/{game-slug}/
_GAME_PATH_RE = re.compile(r"^/roms/([^/]+)/([^/]+)/?$")


class RomsfunSource(RomSource):
    source_id = "romsfun"
    name = "ROMsFun"

    async def search(self, query: str, system: str = "") -> list[dict]:
        sys_slug = _SYSTEM_MAP.get(system, "")

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(
                    f"{ROMSFUN_BASE}/search",
                    params={"q": query},
                    headers=_HEADERS,
                    timeout=20,
                )
                resp.raise_for_status()
        except Exception:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=_GAME_PATH_RE):
            m = _GAME_PATH_RE.match(a["href"])
            if not m:
                continue

            page_sys, page_slug = m.group(1), m.group(2)

            if sys_slug and page_sys != sys_slug:
                continue

            identifier = f"{page_sys}/{page_slug}"
            if identifier in seen:
                continue
            seen.add(identifier)

            title = a.get_text(strip=True)
            if not title:
                h = a.find(["h2", "h3", "h4", "span", "p"])
                title = h.get_text(strip=True) if h else ""
            if not title:
                title = page_slug.replace("-", " ").title()

            results.append({
                "identifier": identifier,
                "title": title,
                "description": "ROMsFun",
                "url": f"{ROMSFUN_BASE}/roms/{identifier}/",
                "source_id": self.source_id,
            })

        return results[:25]

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(
                    f"{ROMSFUN_BASE}/roms/{identifier}/",
                    headers=_HEADERS,
                    timeout=15,
                )
                resp.raise_for_status()
        except Exception:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        h = soup.find("h1") or soup.find("h2")
        game_title = h.get_text(strip=True) if h else identifier.split("/")[-1].replace("-", " ").title()

        size = 0
        for text in soup.stripped_strings:
            if "MB" in text or "GB" in text:
                try:
                    num = float(text.split()[0].replace(",", ""))
                    size = int(num * (1024 ** 3 if "GB" in text else 1024 ** 2))
                    break
                except (ValueError, IndexError):
                    pass

        safe = game_title.replace(":", " -").replace("/", "-")
        filename = f"{safe}.zip"

        if name_filter and name_filter.lower() not in filename.lower():
            return []

        return [{
            "name": filename,
            "identifier": identifier,
            "source_id": self.source_id,
            "size": size,
            "md5": "",
        }]

    def get_download_url(self, identifier: str, filename: str) -> str:
        parts = identifier.split("/", 1)
        if len(parts) == 2:
            return f"{ROMSFUN_BASE}/download/{parts[0]}/{parts[1]}/"
        return f"{ROMSFUN_BASE}/roms/{identifier}/"

    def get_extra_headers(self) -> dict:
        return _HEADERS


SOURCE_CLASS = RomsfunSource
