"""VIMM's Lair ROM source.

Scrapes vimm.net search and game pages.
Downloads use the vimm.net download endpoint with a Referer header.
"""

import httpx
from bs4 import BeautifulSoup

from .base import RomSource

VIMM_BASE = "https://vimm.net"
VIMM_DOWNLOAD_BASE = "https://download3.vimm.net/download"

# Map RA/display system name → VIMM system slug
_SYSTEM_MAP: dict[str, str] = {
    "NES": "NES",
    "SNES": "SNES",
    "Nintendo 64": "N64",
    "Game Boy": "GB",
    "Game Boy Advance": "GBA",
    "Game Boy Color": "GBC",
    "Nintendo DS": "DS",
    "Nintendo DSi": "DS",
    "GameCube": "GameCube",
    "Sega Genesis / Mega Drive": "Genesis",
    "Sega CD": "SegaCD",
    "Sega 32X": "32X",
    "Saturn": "Saturn",
    "Dreamcast": "Dreamcast",
    "Master System": "SMS",
    "Game Gear": "GameGear",
    "SG-1000": "SG1000",
    "PlayStation": "PS1",
    "PlayStation 2": "PS2",
    "PlayStation Portable": "PSP",
    "Atari 2600": "Atari2600",
    "Atari 5200": "Atari5200",
    "Atari 7800": "Atari7800",
    "Atari Lynx": "Lynx",
    "PC Engine / TurboGrafx-16": "TurboGrafx",
    "PC Engine CD": "TurboGrafxCD",
    "Neo Geo Pocket": "NGP",
    "WonderSwan": "WonderSwan",
    "3DO Interactive Multiplayer": "3DO",
    "Virtual Boy": "VirtualBoy",
    "Vectrex": "Vectrex",
}


class VimmSource(RomSource):
    source_id = "vimm"
    name = "VIMM's Lair"

    def _vimm_system(self, system: str) -> str:
        """Convert a display system name to VIMM's system slug."""
        return _SYSTEM_MAP.get(system, "")

    async def search(self, query: str, system: str = "") -> list[dict]:
        vimm_sys = self._vimm_system(system)
        params: dict = {"p": "list", "q": query}
        if vimm_sys:
            params["system"] = vimm_sys

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                f"{VIMM_BASE}/vault/",
                params=params,
                headers={"User-Agent": "Mozilla/5.0 (compatible; rom-finder/1.0)"},
                timeout=20,
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []

        # VIMM lists games in a table; each row links to /vault/{mediaId}/
        for row in soup.select("table.rounded tr"):
            link = row.find("a", href=True)
            if not link:
                continue
            href = link["href"]
            # Game URLs look like /vault/1234/ or /vault/1234
            parts = [p for p in href.strip("/").split("/") if p]
            if len(parts) == 2 and parts[0] == "vault" and parts[1].isdigit():
                media_id = parts[1]
                title = link.get_text(strip=True)
                if title:
                    results.append({
                        "identifier": media_id,
                        "title": title,
                        "description": f"VIMM's Lair — Media ID {media_id}",
                        "source_id": self.source_id,
                    })

        return results

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        """Fetch the VIMM game page and extract ROM file info."""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                f"{VIMM_BASE}/vault/{identifier}/",
                headers={"User-Agent": "Mozilla/5.0 (compatible; rom-finder/1.0)"},
                timeout=15,
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Game title from <h2> or <title>
        h2 = soup.find("h2")
        game_title = h2.get_text(strip=True) if h2 else f"VIMM {identifier}"

        # File size often appears near the download section
        size = 0
        for tag in soup.find_all(string=True):
            text = tag.strip()
            if "MB" in text or "GB" in text:
                try:
                    num = float(text.split()[0].replace(",", ""))
                    if "GB" in text:
                        size = int(num * 1024 * 1024 * 1024)
                    else:
                        size = int(num * 1024 * 1024)
                    break
                except (ValueError, IndexError):
                    pass

        # Derive a plausible filename from the game title
        safe_title = game_title.replace(":", " -").replace("/", "-")
        filename = f"{safe_title}.zip"

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
        return f"{VIMM_DOWNLOAD_BASE}/?mediaId={identifier}"

    def get_extra_headers(self) -> dict:
        return {
            "Referer": f"{VIMM_BASE}/",
            "User-Agent": "Mozilla/5.0 (compatible; rom-finder/1.0)",
        }
