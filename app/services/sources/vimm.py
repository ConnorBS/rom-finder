"""VIMM's Lair ROM source.

Scrapes vimm.net search and game pages.
Downloads use the vimm.net download endpoint with a Referer header.
"""

import re
import httpx
from bs4 import BeautifulSoup

from .base import RomSource

VIMM_BASE = "https://vimm.net"
VIMM_DOWNLOAD_BASE = "https://download3.vimm.net/download"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

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

_VAULT_RE = re.compile(r"^/vault/(\d+)/?$")


class VimmSource(RomSource):
    source_id = "vimm"
    name = "VIMM's Lair"

    def _vimm_system(self, system: str) -> str:
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
                headers=_HEADERS,
                timeout=20,
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict] = []
        seen_ids: set[str] = set()

        # Vimm search results are a div/list layout.
        # Each game entry contains an <a href="/vault/{id}"> link and
        # a flag image at /images/flags/{country}.png with the region as alt text.
        for a in soup.find_all("a", href=_VAULT_RE):
            m = _VAULT_RE.match(a["href"])
            if not m:
                continue
            media_id = m.group(1)
            if media_id in seen_ids:
                continue
            seen_ids.add(media_id)

            title = a.get_text(strip=True)
            if not title:
                continue

            # Region: flag image in the same row/container as the link
            region = ""
            container = a.parent
            if container:
                flag = container.find("img", src=re.compile(r"/images/flags/"))
                if flag:
                    region = flag.get("alt", "").strip()

            results.append({
                "identifier": media_id,
                "title": title,
                "description": "VIMM's Lair",
                "region": region,
                "url": f"{VIMM_BASE}/vault/{media_id}/",
                "source_id": self.source_id,
            })

        # If system filter returned nothing, retry without it so partial title
        # matches still surface (e.g. multi-disc sets labelled differently).
        if not results and vimm_sys:
            return await self.search(query, "")

        return results

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                f"{VIMM_BASE}/vault/{identifier}/",
                headers=_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        h2 = soup.find("h2")
        game_title = h2.get_text(strip=True) if h2 else f"VIMM {identifier}"

        size = 0
        for text in soup.stripped_strings:
            if "MB" in text or "GB" in text:
                try:
                    num = float(text.split()[0].replace(",", ""))
                    size = int(num * (1024 ** 3 if "GB" in text else 1024 ** 2))
                    break
                except (ValueError, IndexError):
                    pass

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
            **_HEADERS,
        }
