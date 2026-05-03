"""CDRomance ROM source — https://cdromance.org

Search and file-listing use plain httpx + BeautifulSoup (no JS needed).
Downloads follow the page's download form / redirect chain with httpx.
If the site requires a JS countdown to reveal the link, the download
raises RuntimeError with a clear message so the caller can skip it.
"""

import asyncio
import logging
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from .base import RomSource

logger = logging.getLogger(__name__)

CDR_BASE = "https://cdromance.org"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

_SYSTEM_MAP: dict[str, str] = {
    # Nintendo
    "NES":                          "nes-roms",
    "SNES":                         "snes-rom",
    "Nintendo 64":                  "n64-roms",
    "Game Boy":                     "gameboy-roms",
    "Game Boy Advance":             "gba-roms",
    "Game Boy Color":               "gameboy-color-roms",
    "Nintendo DS":                  "nds-roms",
    "GameCube":                     "gamecube",
    "Wii":                          "wii-iso",
    # Sega
    "Sega Genesis / Mega Drive":    "sega_genesis_roms",
    "Sega CD":                      "sega_cd_isos",
    "Sega 32X":                     "sega_32x_roms",
    "Saturn":                       "sega_saturn_isos",
    "Dreamcast":                    "dc-iso",
    "Master System":                "sms_roms",
    "Game Gear":                    "game-gear",
    # Sony
    "PlayStation":                  "psx-iso",
    "PlayStation 2":                "ps2-iso",
    "PlayStation Portable":         "psp",
    # NEC
    "PC Engine / TurboGrafx-16":   "turbografx-16",
    "PC Engine CD":                 "turbografx-cd",
    # SNK
    "Neo Geo Pocket":               "neo-geo-pocket",
    # Other
    "3DO Interactive Multiplayer":  "3do-iso",
    "WonderSwan":                   "wonderswan",
    "MSX":                          "msx-roms",
}

_FILE_EXTS = {
    ".iso", ".bin", ".cue", ".img", ".chd",
    ".zip", ".7z", ".rar",
    ".nes", ".sfc", ".smc", ".gb", ".gbc", ".gba",
    ".n64", ".z64", ".v64", ".nds",
    ".md", ".gen", ".sms", ".gg",
    ".cso", ".pbp", ".psp",
}

_SIZE_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB)", re.IGNORECASE)


def _parse_size(s: str) -> int:
    m = _SIZE_RE.search(s)
    if not m:
        return 0
    v, u = float(m.group(1)), m.group(2).upper()
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(v * mult[u])


def _content_tags(identifier: str) -> str:
    """Return short tag string for translations/romhacks/undubs."""
    p = identifier.lower()
    if "translations" in p or "english-patched" in p:
        return "[Translation]"
    if "undub" in p:
        return "[Undub]"
    if "romhack" in p:
        return "[Romhack]"
    return ""


def _is_disc_slug(slug: str) -> bool:
    disc = {"psx-iso", "ps2-iso", "dc-iso", "sega_saturn_isos", "sega_cd_isos",
            "wii-iso", "gamecube", "3do-iso", "turbografx-cd", "psp"}
    return slug in disc


class CdromanceSource(RomSource):
    source_id = "cdromance"
    name = "CDRomance"

    async def search(self, query: str, system: str = "") -> list[dict]:
        slug = _SYSTEM_MAP.get(system, "")
        url = f"{CDR_BASE}/{slug}/?s={query}" if slug else f"{CDR_BASE}/?s={query}"

        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, follow_redirects=True, timeout=20
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("CDRomance search failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict] = []

        for article in soup.select("article"):
            link_el = article.select_one("h2 a, h3 a, .entry-title a")
            if not link_el:
                continue
            title = link_el.get_text(strip=True)
            href = link_el.get("href", "")
            if not href or CDR_BASE not in href:
                continue

            identifier = href.replace(CDR_BASE, "").strip("/")
            tag = _content_tags(identifier)

            # Platform shown in article meta
            platform_el = article.select_one(".platform, .console, .cat-badge, .category-badge")
            platform = platform_el.get_text(strip=True) if platform_el else system

            desc_parts = []
            if platform:
                desc_parts.append(f"[{platform}]")
            if tag:
                desc_parts.append(tag)

            results.append({
                "identifier": identifier,
                "title": title,
                "description": " ".join(desc_parts),
                "source_id": self.source_id,
            })

        return results

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        page_url = f"{CDR_BASE}/{identifier}/"
        await asyncio.sleep(0.5)  # polite delay between requests

        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, follow_redirects=True, timeout=20
            ) as client:
                resp = await client.get(page_url)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("CDRomance get_files failed for %s: %s", identifier, exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        filename = ""
        size = 0

        # Try info table rows
        for row in soup.select("table tr, .game-info tr, .entry-content tr"):
            cells = row.select("td, th")
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True).lower()
            value = cells[1].get_text(strip=True)
            if any(k in label for k in ("file name", "filename", "file")):
                if not filename:
                    filename = value
            elif "size" in label and not size:
                size = _parse_size(value)

        # Fall back: infer filename from page title + platform slug
        if not filename:
            h1 = soup.select_one("h1.entry-title, h1, h2.entry-title")
            if h1:
                base = h1.get_text(strip=True)
                slug = identifier.split("/")[0] if "/" in identifier else identifier
                ext = ".iso" if _is_disc_slug(slug) else ".zip"
                filename = f"{base}{ext}"

        if not filename:
            return []

        if name_filter:
            nf = Path(name_filter).stem.lower()
            if nf not in Path(filename).stem.lower():
                return []

        return [{
            "name": filename,
            "identifier": identifier,
            "source_id": self.source_id,
            "size": size,
        }]

    def get_download_url(self, identifier: str, filename: str) -> str:
        # The game page URL is the entry point; download_file() handles navigation.
        return f"{CDR_BASE}/{identifier}/"

    async def download_file(
        self,
        url: str,
        dest: Path,
        progress_callback=None,
    ) -> None:
        """Fetch the game page, find the download link/form, and stream the file."""
        dest.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30, read=None, write=30, pool=30),
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Strategy 1: direct file link in an <a> tag
            dl_url = self._find_direct_link(soup)
            if dl_url:
                await self._stream(client, dl_url, dest, progress_callback)
                return

            # Strategy 2: submit the download form
            form = self._find_download_form(soup)
            if form:
                action = form.get("action") or url
                if not action.startswith("http"):
                    action = CDR_BASE + "/" + action.lstrip("/")
                fields = {
                    inp["name"]: inp.get("value", "")
                    for inp in form.select("input[name]")
                    if inp.get("name")
                }
                post_resp = await client.post(action, data=fields)

                # If we got a file directly, stream it
                ctype = post_resp.headers.get("content-type", "")
                if "html" not in ctype:
                    await self._stream_response(post_resp, dest, progress_callback)
                    return

                # POST returned HTML — look for a download link in it
                soup2 = BeautifulSoup(post_resp.text, "html.parser")
                dl_url = self._find_direct_link(soup2)
                if dl_url:
                    await self._stream(client, dl_url, dest, progress_callback)
                    return

            raise RuntimeError(
                f"CDRomance: no direct download link found on {url}. "
                "The page may require a browser to trigger the download."
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_direct_link(self, soup: BeautifulSoup) -> str:
        """Return the first href that looks like a ROM file, or ''."""
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            text = a.get_text(strip=True).lower()
            # Explicit file extension
            if Path(href.split("?")[0]).suffix.lower() in _FILE_EXTS:
                return href
            # Link labelled "download" pointing off-site or to a /download/ path
            if "download" in text and ("download" in href or href.startswith("http")):
                if not href.startswith(CDR_BASE) or "/download/" in href:
                    return href
        return ""

    def _find_download_form(self, soup: BeautifulSoup):
        """Return a <form> that contains a download button, or None."""
        for form in soup.select("form"):
            action = (form.get("action") or "").lower()
            if "download" in action:
                return form
            btn = form.select_one("button, input[type='submit']")
            if btn:
                label = (btn.get_text(strip=True) + btn.get("value", "")).lower()
                if "download" in label:
                    return form
        return None

    async def _stream(
        self, client: httpx.AsyncClient, url: str, dest: Path, progress_callback
    ) -> None:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            await self._stream_response(resp, dest, progress_callback)

    @staticmethod
    async def _stream_response(resp, dest: Path, progress_callback) -> None:
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as fh:
            async for chunk in resp.aiter_bytes(65536):
                fh.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total:
                    await progress_callback(downloaded / total)
