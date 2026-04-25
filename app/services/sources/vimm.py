"""VIMM's Lair ROM source.

Scrapes vimm.net search and game pages.
Downloads POST to Vimm's download CDN with a mediaId and Referer header.
"""

import re
import httpx
from bs4 import BeautifulSoup
from pathlib import Path

from .base import RomSource

VIMM_BASE = "https://vimm.net"

# Fallback download server — the vault page's dl_form action is preferred when
# the game is actually available (parsed dynamically in download_file).
# dl3.vimm.net is the current CDN; different platforms may use different nodes
# (e.g. download5 for GameCube), which is why we always prefer the form action.
VIMM_DOWNLOAD_FALLBACK = "https://dl3.vimm.net/"

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
    "Wii": "Wii",
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

# Matches /vault/8003, /vault/8003/, or /vault/8003/Burnout-3-Takedown/
_VAULT_ID_RE = re.compile(r"/vault/(\d+)")


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

        for a in soup.find_all("a", href=_VAULT_ID_RE):
            m = _VAULT_ID_RE.search(a["href"])
            if not m:
                continue
            media_id = m.group(1)
            if media_id in seen_ids:
                continue
            seen_ids.add(media_id)

            title = a.get_text(strip=True)
            if not title:
                continue

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

        # If system filter returned nothing, retry without it.
        if not results and vimm_sys:
            return await self.search(query, "")

        # If still nothing and query has multiple words, progressively drop the
        # last word (handles long subtitles like "Freddi Fish: The Case of ...").
        if not results:
            words = query.split()
            if len(words) > 1:
                shorter = " ".join(words[:-1])
                if len(shorter) >= 3:
                    return await self.search(shorter, system)

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

        # Check if download is available by looking for the download form
        dl_form = soup.find("form", {"name": "dl_form"}) or soup.find("form", {"id": "dl_form"})
        if not dl_form:
            # No download form — game is unavailable (DMCA or not yet uploaded)
            return []

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

        # Stem-based bidirectional match: the Vimm filename is typically a clean
        # title without region codes, while the RA name may include "(USA)" etc.
        if name_filter:
            filter_stem = Path(name_filter).stem.lower()
            file_stem = Path(filename).stem.lower()
            if filter_stem not in file_stem and file_stem not in filter_stem:
                return []

        return [{
            "name": filename,
            "identifier": identifier,
            "source_id": self.source_id,
            "size": size,
            "md5": "",
        }]

    def get_download_url(self, identifier: str, filename: str) -> str:
        # Encodes the mediaId so download_file() can extract it.
        # Actual download uses POST — see download_file() override.
        return f"{VIMM_DOWNLOAD_FALLBACK}?mediaId={identifier}"

    def get_extra_headers(self) -> dict:
        return {
            "Referer": f"{VIMM_BASE}/",
            **_HEADERS,
        }

    async def download_file(
        self,
        url: str,
        dest: Path,
        progress_callback=None,
    ) -> None:
        """Download from Vimm by:
        1. Visiting the vault page to acquire session cookies and the real
           download form action URL (which may vary by CDN node).
        2. POSTing mediaId + alt=0 to that action URL with a Referer header.
        """
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        media_id = params.get("mediaId", [None])[0]
        if not media_id:
            raise ValueError(f"Cannot extract mediaId from Vimm URL: {url}")

        vault_url = f"{VIMM_BASE}/vault/{media_id}/"
        download_action = VIMM_DOWNLOAD_FALLBACK

        dest.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
            # Visit vault page to get cookies AND extract the real form action URL
            # and the actual mediaId (which differs from the vault ID in the URL).
            post_media_id = media_id  # vault ID fallback; form value overrides below
            try:
                vault_resp = await client.get(vault_url, headers=_HEADERS, timeout=20)
                soup = BeautifulSoup(vault_resp.text, "html.parser")
                form = (
                    soup.find("form", {"name": "dl_form"})
                    or soup.find("form", {"id": "dl_form"})
                )
                if form is None:
                    raise ValueError(
                        f"No download form found for vault ID {media_id}. "
                        "The game may be unavailable or removed due to a takedown request."
                    )
                if form.get("action"):
                    action = form["action"]
                    if action.startswith("//"):
                        action = "https:" + action
                    elif not action.startswith("http"):
                        action = "https:" + action
                    download_action = action

                # The form's mediaId is the actual file identifier — it is NOT the
                # same as the vault ID in the page URL.
                form_media_input = form.find("input", {"name": "mediaId"})
                if form_media_input and form_media_input.get("value"):
                    post_media_id = form_media_input["value"]

            except ValueError:
                raise
            except Exception:
                pass  # network error visiting vault page — try download anyway

            post_headers = {
                **_HEADERS,
                "Referer": vault_url,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": VIMM_BASE,
            }
            post_data = {"mediaId": post_media_id, "alt": "0"}

            async with client.stream(
                "POST",
                download_action,
                data=post_data,
                headers=post_headers,
            ) as resp:
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    # Vimm returned an error/login page instead of the file
                    body = await resp.aread()
                    raise ValueError(
                        "Vimm returned an HTML page instead of the ROM file. "
                        "The game may be unavailable, rate-limited, or require login."
                    )

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            await progress_callback(downloaded / total)
