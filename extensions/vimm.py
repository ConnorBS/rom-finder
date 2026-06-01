"""Vimm's Lair ROM source extension for ROM Finder."""

EXTENSION_INFO = {
    "id": "vimm",
    "name": "Vimm's Lair",
    "version": "1.4.0",
    "type": "rom_source",
    "author": "ConnorBS",
    "description": (
        "Downloads ROMs from Vimm's Lair. Uses headless Chromium (Playwright) for search, "
        "file listing AND download — the whole site now bot-blocks plain HTTP clients, so "
        "httpx requests time out. Enforces Vimm's one-download-at-a-time policy automatically."
    ),
}

import asyncio
import re
from bs4 import BeautifulSoup
from pathlib import Path

from app.services.sources.base import RomSource

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


async def _browser_get_html(url: str, timeout_ms: int = 30000) -> str:
    """Fetch a Vimm page's rendered HTML via headless Chromium.

    Vimm now bot-blocks plain HTTP clients site-wide — httpx requests to the
    homepage, a vault game page, AND the search listing all hang (ReadTimeout),
    so search/get_files (which used httpx) silently returned nothing and Vimm
    contributed zero candidates to every hunt. Only a real browser gets a
    response, so route page fetches through Chromium like downloads already do.
    Callers hold `_get_vimm_lock()` so we never run more than one Vimm browser.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed — Vimm's Lair is unavailable. "
            "Install Playwright in the Docker container to enable Vimm."
        )
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_HEADERS["User-Agent"])
        page = await context.new_page()
        try:
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            return await page.content()
        finally:
            await context.close()
            await browser.close()


VIMM_BASE = "https://vimm.net"

# Used only to encode the vault mediaId into a parseable URL for get_download_url().
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

# Matches a vault GAME url: /vault/8003, /vault/8003/, or
# /vault/8003/Burnout-3-Takedown/ — the id must be the COMPLETE path segment
# (followed by /, ?, # or end of string). This deliberately does NOT match
# console-category links like /vault/32X or /vault/3DO, whose digit prefix
# ("32", "3") was previously captured as a bogus media id and surfaced as
# fake "Sega 32X" / "3DO" search results.
_VAULT_ID_RE = re.compile(r"/vault/(\d+)(?=[/?#]|$)")


class VimmSource(RomSource):
    source_id = "vimm"
    name = "VIMM's Lair"

    def _vimm_system(self, system: str) -> str:
        return _SYSTEM_MAP.get(system, "")

    async def search(self, query: str, system: str = "") -> list[dict]:
        vimm_sys = self._vimm_system(system)
        if not vimm_sys:
            return []  # Vimm search requires a known system; unknown system = no results

        # Serialize Vimm browser use, then fetch the listing (with one no-system
        # fallback). The old httpx word-dropping recursion is gone: it was a
        # workaround for httpx returning nothing, and with a real browser each
        # extra try is a full Chromium launch — the hunt already retries with
        # several queries (RA ROM-name stems + title variants), so Vimm gets
        # multiple shots without launching a browser per dropped word.
        async with _get_vimm_lock():
            results = await self._search_once(query, vimm_sys)
            if not results:
                results = await self._search_once(query, "")
        return results

    async def _search_once(self, query: str, vimm_sys: str) -> list[dict]:
        from urllib.parse import urlencode

        params: dict = {"p": "list", "q": query}
        if vimm_sys:
            params["system"] = vimm_sys
        url = f"{VIMM_BASE}/vault/?{urlencode(params)}"

        try:
            html = await _browser_get_html(url)
        except Exception:
            return []

        soup = BeautifulSoup(html, "html.parser")
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

        return results

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        # Fetch the vault game page via Chromium (httpx is bot-blocked site-wide).
        async with _get_vimm_lock():
            html = await _browser_get_html(f"{VIMM_BASE}/vault/{identifier}/")

        soup = BeautifulSoup(html, "html.parser")

        h2 = soup.find("h2")
        game_title = h2.get_text(strip=True) if h2 else f"VIMM {identifier}"

        dl_form = soup.find("form", {"name": "dl_form"}) or soup.find("form", {"id": "dl_form"})
        if not dl_form:
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


SOURCE_CLASS = VimmSource
