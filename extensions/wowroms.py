"""WowROMs ROM source extension for ROM Finder."""

EXTENSION_INFO = {
    "id": "wowroms",
    "name": "WowROMs",
    "version": "1.1.0",
    "type": "rom_source",
    "author": "ConnorBS",
    "description": "Downloads ROMs from WowROMs.com. No Playwright required.",
}

EXTENSION_SETTINGS = []

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app.services.sources.base import RomSource

logger = logging.getLogger(__name__)

WOWROMS_BASE = "https://www.wowroms.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# RA system name → WowROMs system slug (as it appears in game page URLs)
_SYSTEM_MAP: dict[str, str] = {
    "NES":                          "nintendo-entertainment-system",
    "SNES":                         "super-nintendo",
    "Nintendo 64":                  "nintendo-64",
    "Game Boy":                     "nintendo-gameboy",
    "Game Boy Color":               "nintendo-gameboy-color",
    "Game Boy Advance":             "nintendo-gameboy-advance",
    "Nintendo DS":                  "nintendo-ds",
    "Virtual Boy":                  "nintendo-virtual-boy",
    "Sega Genesis / Mega Drive":    "sega-genesis-megadrive",
    "Sega 32X":                     "sega-32x",
    "Master System":                "sega-master-system",
    "Game Gear":                    "sega-game-gear",
    "Atari 2600":                   "atari-2600",
    "Atari 5200":                   "atari-5200",
    "Atari 7800":                   "atari-7800",
    "Atari Lynx":                   "atari-lynx",
    "PC Engine / TurboGrafx-16":   "pc-engine-turbografx-16",
    "Neo Geo Pocket":               "neo-geo-pocket",
    "Neo Geo Pocket Color":         "neo-geo-pocket-color",
    "WonderSwan":                   "bandai-wonderswan",
    "WonderSwan Color":             "bandai-wonderswan-color",
}

_AJAX_URL_RE = re.compile(r'var\s+ajaxLinkUrl\s*=\s*["\']([^"\']+)["\']')
_FILE_INPUT_RE = re.compile(r'<input[^>]+name=["\']file["\'][^>]+value=["\']([^"\']+)["\']', re.I)
_SIZE_RE = re.compile(r'([\d,.]+)\s*(kb|mb|gb)', re.I)


def _parse_size(text: str) -> int:
    m = _SIZE_RE.search(text)
    if not m:
        return 0
    v = float(m.group(1).replace(",", "."))
    u = m.group(2).upper()
    return int(v * {"KB": 1024, "MB": 1024**2, "GB": 1024**3}[u])


def _make_token() -> tuple[str, str]:
    """Return (k, t) where k=ms timestamp, t=md5(k) — mirrors WowROMs JS."""
    k = str(int(time.time() * 1000))
    t = hashlib.md5(k.encode()).hexdigest()
    return k, t


class WowromsSource(RomSource):
    source_id = "wowroms"
    name = "WowROMs"

    # ------------------------------------------------------------------
    # Search — GET /en/roms/list?search={query}
    # ------------------------------------------------------------------

    async def search(self, query: str, system: str = "") -> list[dict]:
        expected_slug = _SYSTEM_MAP.get(system, "")
        search_url = f"{WOWROMS_BASE}/en/roms/list?search={query.replace(' ', '+')}"

        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, follow_redirects=True, timeout=20
            ) as client:
                resp = await client.get(search_url)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("WowROMs search failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        # Sidebar has id="prodcoll" — exclude it by only selecting from main col
        main = soup.find(class_="col-md-19") or soup
        results: list[dict] = []
        seen: set[str] = set()

        for card in main.select(".borderp.bgtcolor"):
            title_a = card.select_one("a.title-5")
            if not title_a:
                continue
            href = title_a.get("href", "")
            # href pattern: /en/roms/{system}/{slug}/{id}.html
            m = re.match(r"/en/roms/([a-z0-9-]+)/([a-z0-9-]+)/(\d+)\.html", href)
            if not m:
                continue

            card_system_slug, game_slug, game_id = m.group(1), m.group(2), m.group(3)

            # Filter by system if requested
            if expected_slug and card_system_slug != expected_slug:
                continue

            identifier = f"{card_system_slug}/{game_slug}/{game_id}"
            if identifier in seen:
                continue
            seen.add(identifier)

            title = title_a.get_text(strip=True)
            if not title:
                continue

            # System display name from the red link
            sys_link = card.select_one("a.col-title.colorRed")
            sys_name = sys_link.get_text(strip=True) if sys_link else card_system_slug.replace("-", " ").title()

            # File size
            size_text = ""
            for a in card.select("a.col-title"):
                text = a.get_text(strip=True)
                if "kb" in text.lower() or "mb" in text.lower():
                    size_text = text
                    break

            results.append({
                "identifier": identifier,
                "title": title,
                "description": sys_name,
                "source_id": self.source_id,
                "_size_text": size_text,
            })

        return results[:30]

    # ------------------------------------------------------------------
    # File listing — fetch download page, extract ajax URL + filename
    # ------------------------------------------------------------------

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        parts = identifier.split("/")
        if len(parts) != 3:
            logger.warning("WowROMs: unexpected identifier format: %s", identifier)
            return []
        system_slug, game_slug, game_id = parts

        # Download page: /en/roms/{system}/download-{slug}/{id}.html
        dl_page_url = f"{WOWROMS_BASE}/en/roms/{system_slug}/download-{game_slug}/{game_id}.html"
        await asyncio.sleep(0.3)

        try:
            async with httpx.AsyncClient(
                headers={**_HEADERS, "Referer": WOWROMS_BASE + "/"},
                follow_redirects=True,
                timeout=20,
            ) as client:
                resp = await client.get(dl_page_url)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("WowROMs download page fetch failed for %s: %s", identifier, exc)
            return []

        html = resp.text

        ajax_m = _AJAX_URL_RE.search(html)
        if not ajax_m:
            logger.warning("WowROMs: no ajaxLinkUrl on %s", dl_page_url)
            return []
        ajax_path = ajax_m.group(1)  # e.g. /en/emulators-roms/download/18145/encoded-title

        file_m = _FILE_INPUT_RE.search(html)
        filename = file_m.group(1) if file_m else f"{game_slug}.zip"

        if name_filter:
            nf = Path(name_filter).stem.lower()
            if nf not in Path(filename).stem.lower():
                return []

        # Parse file size from the download page
        soup = BeautifulSoup(html, "html.parser")
        size = 0
        for tag in soup.select("b"):
            text = tag.get_text(strip=True)
            if "kb" in text.lower() or "mb" in text.lower():
                size = _parse_size(text)
                if size:
                    break

        return [{
            "name": filename,
            # Store the ajax path as identifier — download_file() uses it
            "identifier": ajax_path,
            "source_id": self.source_id,
            "size": size,
        }]

    # ------------------------------------------------------------------
    # Download URL — the ajax path is stored as the file identifier
    # ------------------------------------------------------------------

    def get_download_url(self, identifier: str, filename: str) -> str:
        # identifier is the ajax path from the download page JS
        if identifier.startswith("/"):
            return WOWROMS_BASE + identifier
        return identifier

    # ------------------------------------------------------------------
    # Download — generate fresh token, POST to ajax, stream CDN URL
    # ------------------------------------------------------------------

    async def download_file(self, url: str, dest: Path, progress_callback=None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        k, t = _make_token()
        ajax_url = f"{url}?k={k}&t={t}"

        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=60
        ) as client:
            try:
                ajax_resp = await client.post(
                    ajax_url,
                    headers={
                        **_HEADERS,
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": WOWROMS_BASE + "/",
                    },
                )
                ajax_resp.raise_for_status()
                data = ajax_resp.json()
            except Exception as exc:
                raise RuntimeError(f"WowROMs AJAX token request failed: {exc}") from exc

            cdn_url = data.get("link", "")
            if not cdn_url or cdn_url in ("/", ""):
                raise RuntimeError(f"WowROMs AJAX returned no download link: {data}")

            try:
                async with client.stream(
                    "GET", cdn_url,
                    headers={**_HEADERS, "Referer": WOWROMS_BASE + "/"},
                    timeout=None,
                ) as stream:
                    stream.raise_for_status()
                    total = int(stream.headers.get("content-length", 0))
                    downloaded = 0
                    with open(dest, "wb") as fh:
                        async for chunk in stream.aiter_bytes(65536):
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total:
                                await progress_callback(downloaded / total)
            except Exception as exc:
                raise RuntimeError(f"WowROMs CDN download failed: {exc}") from exc


SOURCE_CLASS = WowromsSource
