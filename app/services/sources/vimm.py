"""VIMM's Lair ROM source.

Scrapes vimm.net search and game pages.
Downloads use a headless Chromium browser (Playwright) to solve Vimm's JS bot
challenge. A module-level lock enforces Vimm's one-download-at-a-time policy.
"""

import asyncio
import re
import httpx
from bs4 import BeautifulSoup
from pathlib import Path

from .base import RomSource

# Lazy-initialised so the Lock is created inside the running event loop.
_VIMM_LOCK: asyncio.Lock | None = None


def _get_vimm_lock() -> asyncio.Lock:
    global _VIMM_LOCK
    if _VIMM_LOCK is None:
        _VIMM_LOCK = asyncio.Lock()
    return _VIMM_LOCK


async def _progress_ticker(callback) -> None:
    """Slowly bump reported progress toward 90% so the UI shows activity."""
    pct = 0.02
    while True:
        await asyncio.sleep(5)
        pct = min(0.90, pct + 0.04)
        try:
            await callback(pct)
        except Exception:
            pass

VIMM_BASE = "https://vimm.net"

# Used only to encode the vault mediaId into a parseable URL for get_download_url().
# The actual CDN host is handled by the browser — we never POST to it directly.
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
        # Encodes the vault/mediaId so _browser_download() can extract it.
        return f"{VIMM_DOWNLOAD_FALLBACK}?mediaId={identifier}"

    async def download_file(
        self,
        url: str,
        dest: Path,
        progress_callback=None,
    ) -> None:
        """Download from Vimm using a headless browser to pass the JS challenge.

        Acquires a process-wide lock first — Vimm only permits one concurrent
        download per IP, so all callers queue here.
        """
        async with _get_vimm_lock():
            await self._browser_download(url, dest, progress_callback)

    async def _browser_download(
        self,
        url: str,
        dest: Path,
        progress_callback=None,
    ) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is required for Vimm downloads. "
                "Run: pip install playwright && playwright install chromium"
            )

        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        media_id = params.get("mediaId", [None])[0]
        if not media_id:
            raise ValueError(f"Cannot extract mediaId from Vimm URL: {url}")

        vault_url = f"{VIMM_BASE}/vault/{media_id}/"
        dest.parent.mkdir(parents=True, exist_ok=True)

        ticker: asyncio.Task | None = None
        if progress_callback:
            ticker = asyncio.create_task(_progress_ticker(progress_callback))
            try:
                await progress_callback(0.02)
            except Exception:
                pass

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    accept_downloads=True,
                    user_agent=_HEADERS["User-Agent"],
                )
                page = await context.new_page()
                try:
                    await page.goto(vault_url, timeout=30000, wait_until="domcontentloaded")

                    form = page.locator("form[name='dl_form'], form#dl_form")
                    if await form.count() == 0:
                        raise ValueError(
                            f"No download form found for vault ID {media_id}. "
                            "The game may be unavailable or removed due to DMCA."
                        )

                    # expect_download timeout = time to START receiving the file (60s).
                    # save_as waits indefinitely for the full download to complete.
                    async with page.expect_download(timeout=60000) as dl_info:
                        await form.locator(
                            "input[type='submit'], button[type='submit']"
                        ).first.click()

                    download = await dl_info.value
                    await download.save_as(str(dest))
                finally:
                    await context.close()
                    await browser.close()

            if ticker:
                ticker.cancel()
            if progress_callback:
                await progress_callback(1.0)

        except Exception:
            if ticker:
                ticker.cancel()
            raise
