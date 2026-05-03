"""CDRomance ROM source extension for ROM Finder."""

EXTENSION_INFO = {
    "id": "cdromance",
    "name": "CDRomance",
    "version": "1.0.0",
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

            # Replace / with :: so the identifier is safe for URL path routing.
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

        rows = dl_soup.select(".tr")
        for row in rows:
            tds = row.select(".td")
            if len(tds) < 2:
                continue
            link = tds[0].select_one("a[href]")
            if not link:
                continue

            cdn_url = link["href"]
            filename = link.get_text(strip=True)
            size = _parse_size(tds[1].get_text(strip=True))

            if name_filter:
                nf = Path(name_filter).stem.lower()
                if nf not in Path(filename).stem.lower():
                    continue

            files.append({
                "name": filename,
                "identifier": cdn_url,
                "source_id": self.source_id,
                "size": size,
            })

        return files

    def get_download_url(self, identifier: str, filename: str) -> str:
        return identifier.replace("::", "/")


SOURCE_CLASS = CdromanceSource
