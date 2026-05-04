"""CDRomance ROM source extension for ROM Finder."""

EXTENSION_INFO = {
    "id": "cdromance",
    "name": "CDRomance",
    "version": "1.1.0",
    "type": "rom_source",
    "author": "ConnorBS",
    "description": (
        "Downloads ROMs and ISOs from CDRomance.org. "
        "No Playwright required — uses CDRomance's AJAX download endpoint directly."
    ),
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

CDR_BASE = "https://cdromance.org"
CDR_AJAX = "https://cdromance.org/wp-content/plugins/cdr-main/public/ajax.php"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_SYSTEM_MAP: dict[str, str] = {
    "NES":                          "nes-roms",
    "SNES":                         "snes-rom",
    "Nintendo 64":                  "n64-roms",
    "Game Boy":                     "gameboy-roms",
    "Game Boy Advance":             "gba-roms",
    "Game Boy Color":               "gameboy-color-roms",
    "Nintendo DS":                  "nds-roms",
    "GameCube":                     "gamecube",
    "Wii":                          "wii-iso",
    "Sega Genesis / Mega Drive":    "sega_genesis_roms",
    "Sega CD":                      "sega_cd_isos",
    "Sega 32X":                     "sega_32x_roms",
    "Saturn":                       "sega_saturn_isos",
    "Dreamcast":                    "dc-iso",
    "Master System":                "sms_roms",
    "Game Gear":                    "game-gear",
    "PlayStation":                  "psx-iso",
    "PlayStation 2":                "ps2-iso",
    "PlayStation Portable":         "psp",
    "PC Engine / TurboGrafx-16":   "turbografx-16",
    "PC Engine CD":                 "turbografx-cd",
    "Neo Geo Pocket":               "neo-geo-pocket",
    "3DO Interactive Multiplayer":  "3do-iso",
    "WonderSwan":                   "wonderswan",
    "MSX":                          "msx-roms",
}

_SIZE_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB)", re.IGNORECASE)


def _parse_size(s: str) -> int:
    m = _SIZE_RE.search(s)
    if not m:
        return 0
    v, u = float(m.group(1)), m.group(2).upper()
    return int(v * {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[u])


def _content_tag(identifier: str) -> str:
    p = identifier.lower()
    if "translations" in p or "english-patched" in p:
        return "[Translation]"
    if "undub" in p:
        return "[Undub]"
    if "romhack" in p:
        return "[Romhack]"
    return ""


def _first_cdn_url(dl_soup: BeautifulSoup, target_filename: str = "") -> str | None:
    """Extract CDN URL from CDRomance AJAX response, preferring target_filename."""
    fallback: str | None = None
    for row in dl_soup.select(".tr"):
        tds = row.select(".td")
        if len(tds) < 2:
            continue
        link = tds[0].select_one("a[href]")
        if not link:
            continue
        fname = link.get_text(strip=True)
        cdn = link["href"]
        if fallback is None:
            fallback = cdn
        if not target_filename or fname == target_filename:
            return cdn
    return fallback


class CdromanceSource(RomSource):
    source_id = "cdromance"
    name = "CDRomance"

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

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

        for card in soup.select(".game-container"):
            link = card.select_one("a.cover-link, .bottom-section a")
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith(CDR_BASE):
                continue

            title_el = card.select_one(".game-title")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            if not title:
                continue

            identifier = href.replace(CDR_BASE, "").strip("/").replace("/", "::")
            tag = _content_tag(identifier)

            lang_el = card.select_one(".lang")
            lang = lang_el.get_text(strip=True) if lang_el else ""
            desc = f"{lang}  {tag}".strip() if (lang or tag) else ""

            results.append({
                "identifier": identifier,
                "title": title,
                "description": desc,
                "source_id": self.source_id,
            })

        return results

    # ------------------------------------------------------------------
    # File listing
    # get_files() stores "{game_identifier}|{post_id}" as the file
    # identifier instead of the CDN URL, because CDN URLs are
    # time-limited tokens that expire before the download starts.
    # download_file() does a fresh AJAX call right before streaming.
    # ------------------------------------------------------------------

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        page_url = f"{CDR_BASE}/{identifier.replace('::', '/')}/"
        await asyncio.sleep(0.5)

        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=20
        ) as client:
            try:
                page_resp = await client.get(page_url)
                page_resp.raise_for_status()
            except Exception as exc:
                logger.warning("CDRomance page fetch failed for %s: %s", identifier, exc)
                return []

            soup = BeautifulSoup(page_resp.text, "html.parser")
            wrapper = soup.select_one("#acf-content-wrapper[data-id]")
            if not wrapper:
                logger.warning("CDRomance: no data-id found on %s", page_url)
                return []
            post_id = wrapper["data-id"]

            try:
                ajax_resp = await client.post(
                    CDR_AJAX,
                    content=f"post_id={post_id}",
                    headers={
                        **_HEADERS,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": page_url,
                        "Origin": CDR_BASE,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=15,
                )
            except Exception as exc:
                logger.warning("CDRomance AJAX failed for %s: %s", identifier, exc)
                return []

        if ajax_resp.status_code != 200 or ajax_resp.text.startswith("error"):
            logger.warning("CDRomance AJAX error for %s: %s", identifier, ajax_resp.text[:100])
            return []

        dl_soup = BeautifulSoup(ajax_resp.text, "html.parser")
        files: list[dict] = []

        for row in dl_soup.select(".tr"):
            tds = row.select(".td")
            if len(tds) < 2:
                continue
            link = tds[0].select_one("a[href]")
            if not link:
                continue

            filename = link.get_text(strip=True)
            size = _parse_size(tds[1].get_text(strip=True))

            if name_filter:
                nf = Path(name_filter).stem.lower()
                if nf not in Path(filename).stem.lower():
                    continue

            # Store the stable game+post identifier so download_file()
            # can do a fresh AJAX call to get a non-expired CDN URL.
            file_ident = f"{identifier}|{post_id}"

            files.append({
                "name": filename,
                "identifier": file_ident,
                "source_id": self.source_id,
                "size": size,
            })

        return files

    # ------------------------------------------------------------------
    # Download URL — encode game identifier + post_id + filename into a
    # pseudo-URL that download_file() can parse back out.
    # ------------------------------------------------------------------

    def get_download_url(self, identifier: str, filename: str) -> str:
        return f"cdr://{identifier}/{urllib.parse.quote(filename, safe='')}"

    # ------------------------------------------------------------------
    # Download — fresh AJAX call at download time to get a valid CDN URL
    # ------------------------------------------------------------------

    async def download_file(self, url: str, dest: Path, progress_callback=None) -> None:
        # Parse: cdr://{game_identifier}|{post_id}/{url-encoded-filename}
        path = url[len("cdr://"):]
        slash = path.rfind("/")
        target_file = urllib.parse.unquote(path[slash + 1:]) if slash >= 0 else ""
        rest = path[:slash] if slash >= 0 else path
        pipe = rest.rfind("|")
        post_id = rest[pipe + 1:] if pipe >= 0 else ""
        game_ident = rest[:pipe] if pipe >= 0 else rest
        page_url = f"{CDR_BASE}/{game_ident.replace('::', '/')}/"

        dest.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=30
        ) as client:
            try:
                ajax_resp = await client.post(
                    CDR_AJAX,
                    content=f"post_id={post_id}",
                    headers={
                        **_HEADERS,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": page_url,
                        "Origin": CDR_BASE,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=15,
                )
            except Exception as exc:
                raise RuntimeError(f"CDRomance AJAX failed at download time: {exc}") from exc

            if ajax_resp.status_code != 200 or ajax_resp.text.startswith("error"):
                raise RuntimeError(f"CDRomance AJAX error: {ajax_resp.text[:100]}")

            dl_soup = BeautifulSoup(ajax_resp.text, "html.parser")
            cdn_url = _first_cdn_url(dl_soup, target_file)
            if not cdn_url:
                raise RuntimeError(f"CDRomance: no download URL in AJAX response for post_id={post_id}")

            try:
                async with client.stream(
                    "GET", cdn_url,
                    headers={**_HEADERS, "Referer": page_url},
                    timeout=None,
                ) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with open(dest, "wb") as fh:
                        async for chunk in resp.aiter_bytes(65536):
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total:
                                await progress_callback(downloaded / total)
            except Exception as exc:
                raise RuntimeError(f"CDRomance CDN stream failed: {exc}") from exc


SOURCE_CLASS = CdromanceSource
