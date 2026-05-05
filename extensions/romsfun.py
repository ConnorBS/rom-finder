"""ROMsFun ROM source extension for ROM Finder."""

EXTENSION_INFO = {
    "id": "romsfun",
    "name": "ROMsFun",
    "version": "1.4.0",
    "type": "rom_source",
    "author": "ConnorBS",
    "description": "Downloads ROMs from ROMsFun.com. No bot protection — streams directly from their CDN.",
}

EXTENSION_SETTINGS = []

import asyncio
import logging
import re
import urllib.parse
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app.services.sources.base import RomSource

logger = logging.getLogger(__name__)

ROMSFUN_BASE = "https://romsfun.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# RA system name → ROMsFun system slug (as it appears in /roms/{slug}/ URLs)
_SYSTEM_MAP: dict[str, str] = {
    "NES":                          "nes",
    "SNES":                         "super-nintendo",
    "Nintendo 64":                  "nintendo-64",
    "Game Boy":                     "game-boy",
    "Game Boy Color":               "game-boy-color",
    "Game Boy Advance":             "game-boy-advance",
    "Nintendo DS":                  "nintendo-ds",
    "GameCube":                     "gamecube",
    "Wii":                          "wii",
    "Sega Genesis / Mega Drive":    "sega-genesis",
    "Sega CD":                      "sega-cd",
    "Sega 32X":                     "sega-32x",
    "Saturn":                       "sega-saturn",
    "Dreamcast":                    "dreamcast",
    "Master System":                "sega-master-system",
    "Game Gear":                    "sega-game-gear",
    "PlayStation":                  "playstation",
    "PlayStation 2":                "playstation-2",
    "PlayStation Portable":         "psp",
    "Atari 2600":                   "atari-2600",
    "Atari 5200":                   "atari-5200",
    "Atari 7800":                   "atari-7800",
    "PC Engine / TurboGrafx-16":   "turbografx-16",
    "Neo Geo Pocket":               "neo-geo-pocket",
    "WonderSwan":                   "wonderswan",
    "Virtual Boy":                  "virtual-boy",
    "Nintendo 3DS":                 "nintendo-3ds",
    "Nintendo Switch":              "nintendo-switch",
}

# /roms/{system}/{slug}.html
_GAME_HREF_RE = re.compile(r"^/roms/([a-z0-9-]+)/([a-z0-9-]+)\.html$")

# /download/{slug}-{id}  or  /download/{slug}-{id}/{n}
_DL_PAGE_RE = re.compile(r"/download/([a-z0-9-]+-\d+)(?:/(\d+))?$")

_DL_LINK_RE = re.compile(r'id=["\']download-link["\'][^>]*href=["\']([^"\']+)["\']')
_DL_LINK_RE2 = re.compile(r'href=["\']([^"\']+)["\'][^>]*id=["\']download-link["\']')


def _extract_filename(cdn_url: str) -> str:
    """Pull the filename from the CDN URL path."""
    path = urllib.parse.urlparse(cdn_url).path
    name = urllib.parse.unquote(path.split("/")[-1])
    return name if name else "rom.zip"


def _extract_dl_link(html: str) -> str | None:
    m = _DL_LINK_RE.search(html) or _DL_LINK_RE2.search(html)
    return m.group(1) if m else None


class RomsfunSource(RomSource):
    source_id = "romsfun"
    name = "ROMsFun"

    # ------------------------------------------------------------------
    # Search — WordPress /?s={query}
    # ------------------------------------------------------------------

    async def search(self, query: str, system: str = "") -> list[dict]:
        expected_slug = _SYSTEM_MAP.get(system, "")
        search_url = f"{ROMSFUN_BASE}/?s={urllib.parse.quote_plus(query)}"

        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, follow_redirects=True, timeout=20
            ) as client:
                resp = await client.get(search_url)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("ROMsFun search failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[dict] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = urllib.parse.urlparse(a["href"]).path
            m = _GAME_HREF_RE.match(href)
            if not m:
                continue

            sys_slug, game_slug = m.group(1), m.group(2)

            if expected_slug and sys_slug != expected_slug:
                continue

            identifier = f"{sys_slug}::{game_slug}"
            if identifier in seen:
                continue
            seen.add(identifier)

            # Title: prefer the h3 in the card, fall back to the <a> text or alt text
            parent = a.find_parent(class_=re.compile(r"bg-white"))
            if parent:
                h = parent.find("h3") or parent.find("h2")
                title = h.get_text(strip=True) if h else ""
            else:
                title = a.get_text(strip=True)

            if not title:
                img = a.find("img")
                title = img.get("alt", "") if img else ""
            if not title:
                title = game_slug.replace("-", " ").title()

            results.append({
                "identifier": identifier,
                "title": title,
                "description": sys_slug.replace("-", " ").title(),
                "source_id": self.source_id,
            })

        return results[:30]

    # ------------------------------------------------------------------
    # File listing — game page → download button → mirror 1 → CDN URL
    # ------------------------------------------------------------------

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        # Support both old "/" format and new "::" format for backwards compat
        sep = "::" if "::" in identifier else "/"
        parts = identifier.split(sep, 1)
        if len(parts) != 2:
            logger.warning("ROMsFun: unexpected identifier: %s", identifier)
            return []
        sys_slug, game_slug = parts

        game_url = f"{ROMSFUN_BASE}/roms/{sys_slug}/{game_slug}.html"
        await asyncio.sleep(0.3)

        try:
            async with httpx.AsyncClient(
                headers={**_HEADERS, "Referer": ROMSFUN_BASE + "/"},
                follow_redirects=True,
                timeout=20,
            ) as client:
                resp = await client.get(game_url)
                resp.raise_for_status()
                game_html = resp.text

                # Extract the download page URL from the "Download ROM" button
                soup = BeautifulSoup(game_html, "html.parser")
                dl_btn = soup.find("a", href=re.compile(r"/download/"))
                if not dl_btn:
                    logger.warning("ROMsFun: no download button on %s", game_url)
                    return []

                dl_page_path = urllib.parse.urlparse(dl_btn["href"]).path.rstrip("/")
                # dl_page_path: /download/{slug}-{id}

                # Fetch mirror 1 to get the CDN URL and filename
                mirror_url = f"{ROMSFUN_BASE}{dl_page_path}/1"
                mirror_resp = await client.get(
                    mirror_url,
                    headers={**_HEADERS, "Referer": f"{ROMSFUN_BASE}{dl_page_path}"},
                )
                mirror_resp.raise_for_status()
                mirror_html = mirror_resp.text

                cdn_url = _extract_dl_link(mirror_html)
                if not cdn_url:
                    logger.warning("ROMsFun: no #download-link found for %s", identifier)
                    return []

                # Make relative/protocol-relative URLs absolute
                if cdn_url.startswith("//"):
                    cdn_url = "https:" + cdn_url
                elif cdn_url.startswith("/"):
                    cdn_url = ROMSFUN_BASE + cdn_url

                filename = _extract_filename(cdn_url)

                if name_filter:
                    nf = Path(name_filter).stem.lower()
                    if nf not in Path(filename).stem.lower():
                        return []

                # Probe CDN for file size (HEAD request — no body downloaded)
                size = 0
                try:
                    head = await client.head(
                        cdn_url,
                        headers={**_HEADERS, "Referer": ROMSFUN_BASE + "/"},
                        timeout=10,
                    )
                    size = int(head.headers.get("content-length", 0))
                except Exception:
                    pass

                # Store the mirror page path so download_file() can fetch a fresh CDN URL
                mirror_path = dl_page_path + "/1"  # e.g. /download/super-mario-5000-301806/1

                return [{
                    "name": filename,
                    "identifier": mirror_path,
                    "source_id": self.source_id,
                    "size": size,
                }]

        except Exception as exc:
            logger.warning("ROMsFun get_files failed for %s: %s", identifier, exc)
            return []

    # ------------------------------------------------------------------
    # Download URL — mirror page path is the identifier
    # ------------------------------------------------------------------

    def get_download_url(self, identifier: str, filename: str) -> str:
        if identifier.startswith("/"):
            return ROMSFUN_BASE + identifier
        return identifier

    # ------------------------------------------------------------------
    # Download — re-fetch mirror page for a fresh CDN token, then stream
    # ------------------------------------------------------------------

    async def download_file(self, url: str, dest: Path, progress_callback=None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=60
        ) as client:
            # Re-fetch the mirror page to get a fresh, valid CDN token
            dl_page_base = re.sub(r"/\d+$", "", url)  # strip mirror number
            try:
                mirror_resp = await client.get(
                    url,
                    headers={**_HEADERS, "Referer": dl_page_base},
                )
                mirror_resp.raise_for_status()
            except Exception as exc:
                raise RuntimeError(f"ROMsFun mirror page fetch failed: {exc}") from exc

            cdn_url = _extract_dl_link(mirror_resp.text)
            if not cdn_url:
                raise RuntimeError(f"ROMsFun: no download link found at {url}")
            # Make relative/protocol-relative URLs absolute
            if cdn_url.startswith("//"):
                cdn_url = "https:" + cdn_url
            elif cdn_url.startswith("/"):
                cdn_url = ROMSFUN_BASE + cdn_url

            try:
                async with client.stream(
                    "GET", cdn_url,
                    headers={**_HEADERS, "Referer": url},
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
                raise RuntimeError(f"ROMsFun CDN stream failed: {exc}") from exc


SOURCE_CLASS = RomsfunSource
