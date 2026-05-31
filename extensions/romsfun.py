"""ROMsFun ROM source extension for ROM Finder."""

EXTENSION_INFO = {
    "id": "romsfun",
    "name": "ROMsFun",
    "version": "1.9.0",
    "type": "rom_source",
    "author": "ConnorBS",
    "description": "Downloads ROMs from ROMsFun.com. Searches the per-system catalog (/roms/{system}/?q=) and reads the signed CDN URL embedded in the download mirror page. ROMsFun is behind Cloudflare — set a FlareSolverr URL to reliably bypass the challenge. The download streams the CDN within the SAME session that minted the token (admin-ajax primary, embedded URL fallback) and retries the anti-leech 403 with backoff.",
}

EXTENSION_SETTINGS = [
    {
        "key": "flaresolverr_url",
        "label": "FlareSolverr URL",
        "type": "text",
        "default": "",
        "description": (
            "Optional. Base URL of a FlareSolverr instance "
            "(e.g. http://192.168.0.81:8191) used to bypass ROMsFun's Cloudflare "
            "challenge when searching. Leave blank to use direct requests."
        ),
    },
]

import asyncio
import logging
import os
import re
import urllib.parse
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app.services.sources.base import RomSource
from app.services.sources.errors import (
    SourceForbiddenError,
    SourceNetworkError,
    classify_status,
)

logger = logging.getLogger(__name__)

ROMSFUN_BASE = "https://romsfun.com"
ROMSFUN_AJAX_URL = f"{ROMSFUN_BASE}/wp-admin/admin-ajax.php"

# CDN streaming: retry the anti-leech 403/429 a few times with backoff. The
# token CDN (sto.romsfast.com) 403s intermittently even on a valid, freshly
# minted token, so one rejection is not a permanent failure.
_DOWNLOAD_ATTEMPTS = 3

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# admin-ajax mints a fresh signed URL bound to the posting session/IP — this is
# the path that downloaded reliably before v1.8.0 switched to the (weaker)
# page-embedded token. It needs the XHR header + the mirror page as Referer.
_AJAX_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
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
    "Wii":                          "nintendo-wii",
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

# Cap on per-file download links resolved per game (one game's page can list
# several regions; the cap just bounds Cloudflare exposure on a pathological page).
_MAX_FILES = 12

# Apostrophes (straight, curly) and a stray backslash that ROMsFun's search box
# injects before an apostrophe.
_APOSTROPHE_RE = re.compile(r"[\\'‘’]")


def _normalize_query(query: str) -> str:
    """ROMsFun's catalog search returns ZERO results for a title containing an
    apostrophe — "Pajama Sam: Don't Fear the Dark" (and the escaped "Don\\'t" the
    search box produces) match nothing, and dropping the apostrophe entirely
    ("Dont") also returns nothing. Replacing it with a SPACE ("Don t") is what
    actually matches. Confirmed live against the site. So: apostrophes/backslash
    → space, then collapse whitespace."""
    return re.sub(r"\s+", " ", _APOSTROPHE_RE.sub(" ", query)).strip()


def _parse_search_results(html: str, expected_slug: str, source_id: str) -> list[dict]:
    """Extract game results from a ROMsFun catalog/search page. Keeps only links
    for the expected system slug (the per-system page can still link sibling
    consoles in 'related' rails)."""
    soup = BeautifulSoup(html, "html.parser")
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
            "source_id": source_id,
        })

    return results[:30]


def _extract_filename(cdn_url: str) -> str:
    """Pull the filename from the CDN URL path (ignores the ?token query)."""
    path = urllib.parse.urlparse(cdn_url).path
    name = urllib.parse.unquote(path.split("/")[-1])
    return name if name else "rom.zip"


def _file_paths(html: str, base: str) -> list[str]:
    """Every per-file download path for this game on its landing page.

    A ROMsFun game page can list more than one ROM (e.g. a USA dump and a
    Europe dump) under the same `{slug}-{id}` base, each at `/download/{base}/{n}`.
    The bare `/download/{base}` form is normalised to `/download/{base}/1`. We
    always include `/1` (the first/default file) so a single-file game still
    resolves even if its own link isn't present in the markup.
    """
    paths: set[str] = set()
    for m in re.finditer(rf"/download/{re.escape(base)}(?:/(\d+))?", html):
        n = m.group(1) or "1"
        paths.add(f"/download/{base}/{n}")
    paths.add(f"/download/{base}/1")
    return sorted(paths, key=lambda p: int(p.rsplit("/", 1)[1]))[:_MAX_FILES]


# Absolute CDN URL to a downloadable ROM, optionally carrying a ?token=… query.
# ROMsFun embeds the signed URL straight in each /download/…/{n} mirror page, so
# we parse it from the HTML rather than POSTing the WordPress admin-ajax endpoint
# — that endpoint identifies the file by Referer header (which FlareSolverr can't
# set) and 403s automated POSTs anyway.
_CDN_FILE_RE = re.compile(
    r'https?://[^\s"\'<>]+\.'
    r'(?:zip|7z|rar|rvz|iso|wbfs|wia|gcz|chd|cso|nds|3ds|nes|sfc|smc|gba|gbc|gb|md|gen|bin)'
    r'(?:\?[^\s"\'<>]*)?',
    re.IGNORECASE,
)


def _parse_cdn_url(html: str) -> str | None:
    """Extract the signed download URL embedded in a ROMsFun mirror page. Prefers
    a token-signed URL — a bare, unsigned URL 403s from the anti-leech CDN."""
    matches = _CDN_FILE_RE.findall(html)
    signed = [u for u in matches if "?" in u]
    if signed:
        return signed[0]
    return matches[0] if matches else None


class RomsfunSource(RomSource):
    source_id = "romsfun"
    name = "ROMsFun"
    # Base URL of an optional FlareSolverr instance (set via the extension
    # setting). "" = talk to ROMsFun directly with httpx.
    _flaresolverr_url = ""

    def configure(self, config: dict) -> None:
        self._flaresolverr_url = (config.get("flaresolverr_url") or "").strip().rstrip("/")

    # ------------------------------------------------------------------
    # HTTP — fetch a page's HTML, via FlareSolverr when configured
    # ------------------------------------------------------------------

    async def _fetch_html(self, url: str) -> str:
        """GET `url` and return its HTML. Routes through FlareSolverr when one is
        configured: the request then originates from FlareSolverr's own IP so it
        actually clears Cloudflare (a cf_clearance cookie is IP-bound and can't
        just be copied to this host). Falls back to direct httpx when no solver
        is set, or if the solver call fails, so a down solver never hard-breaks
        search."""
        if self._flaresolverr_url:
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    resp = await client.post(
                        f"{self._flaresolverr_url}/v1",
                        json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
                    )
                    resp.raise_for_status()
                    html = resp.json().get("solution", {}).get("response", "")
                if html:
                    return html
                logger.warning("ROMsFun: FlareSolverr returned empty body for %s", url)
            except Exception as exc:
                logger.warning("ROMsFun: FlareSolverr request failed (%s); using direct httpx", exc)

        async with httpx.AsyncClient(
            headers={**_HEADERS, "Referer": ROMSFUN_BASE + "/"},
            follow_redirects=True, timeout=20,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Search — per-system catalog /roms/{system}/?q={query}
    # ------------------------------------------------------------------

    async def search(self, query: str, system: str = "") -> list[dict]:
        expected_slug = _SYSTEM_MAP.get(system, "")
        norm = _normalize_query(query)

        # The per-system catalog search matches accurately; the global ?s= search
        # is result-capped and buries/misses titles (it surfaced a *different*
        # 'Pajama Sam' game while "Don't Fear the Dark" — which exists — never
        # appeared). Fall back to ?s= only when the system is unknown.
        if expected_slug:
            search_url = f"{ROMSFUN_BASE}/roms/{expected_slug}/?q={urllib.parse.quote_plus(norm)}"
        else:
            search_url = f"{ROMSFUN_BASE}/?s={urllib.parse.quote_plus(norm)}"

        try:
            html = await self._fetch_html(search_url)
        except Exception as exc:
            logger.warning("ROMsFun search failed: %s", exc)
            return []

        return _parse_search_results(html, expected_slug, self.source_id)

    # ------------------------------------------------------------------
    # File listing — game page → landing page → per-file mirror → signed CDN URL
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
            # Game + mirror pages are Cloudflare-protected → fetch via FlareSolverr
            # when configured.
            game_html = await self._fetch_html(game_url)
            dl_btn = BeautifulSoup(game_html, "html.parser").find(
                "a", href=re.compile(r"/download/")
            )
            if not dl_btn:
                logger.warning("ROMsFun: no download button on %s", game_url)
                return []

            dl_page_path = urllib.parse.urlparse(dl_btn["href"]).path.rstrip("/")
            base_m = _DL_PAGE_RE.search(dl_page_path)
            if not base_m:
                logger.warning("ROMsFun: unparseable download path %s", dl_page_path)
                return []
            base = base_m.group(1)  # "{slug}-{id}" without any mirror number

            # The landing page lists every region/file for this game; a game can
            # have a USA *and* a Europe dump and only one may be the RA-accepted
            # hash, so enumerate them all.
            landing_html = await self._fetch_html(f"{ROMSFUN_BASE}/download/{base}")
            file_paths = _file_paths(landing_html, base)

            nf = Path(name_filter).stem.lower() if name_filter else ""
            files: list[dict] = []
            seen_names: set[str] = set()

            for mirror_path in file_paths:
                mirror_url = ROMSFUN_BASE + mirror_path
                # Each per-file mirror page embeds its own signed CDN URL (so /1
                # and /2 yield different files) — parse it from the HTML rather
                # than the Referer-bound admin-ajax POST (which 403s through
                # FlareSolverr and can't set the Referer it needs).
                cdn_url = _parse_cdn_url(await self._fetch_html(mirror_url))
                if not cdn_url:
                    continue

                filename = _extract_filename(cdn_url)
                # Dedup by resolved filename — handles the case where the mirror
                # numbers turn out to be alternate mirrors of one file, not
                # distinct regions (same CDN filename → keep one).
                if filename in seen_names:
                    continue
                seen_names.add(filename)

                if nf:
                    fstem = Path(filename).stem.lower()
                    if nf not in fstem and fstem not in nf:
                        continue

                size = 0
                try:
                    async with httpx.AsyncClient(
                        headers={**_HEADERS, "Referer": mirror_url},
                        follow_redirects=True, timeout=10,
                    ) as client:
                        head = await client.head(cdn_url)
                        size = int(head.headers.get("content-length", 0))
                except Exception:
                    pass

                files.append({
                    "name": filename,
                    # Store the mirror path — download_file() re-parses a fresh
                    # signed URL at download time (the token can expire).
                    "identifier": mirror_path,
                    "source_id": self.source_id,
                    "size": size,
                })
                await asyncio.sleep(0.2)  # be gentle with Cloudflare

            if not files:
                logger.warning("ROMsFun: no resolvable files for %s", identifier)
            return files

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
    # Download — mint a fresh signed URL in-session, then stream the CDN
    # ------------------------------------------------------------------

    async def _ajax_signed_url(self, client: httpx.AsyncClient, mirror_url: str) -> str | None:
        """Mint a fresh signed CDN URL via the WordPress admin-ajax endpoint on
        `client` (so it carries the session cookie set by visiting the mirror
        page). The server identifies the file by the mirror-page Referer."""
        try:
            resp = await client.post(
                ROMSFUN_AJAX_URL,
                headers={**_AJAX_HEADERS, "Referer": mirror_url},
                data={"action": "k_get_download"},
                timeout=20,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("success"):
                    return (data.get("data") or {}).get("download_url")
        except Exception as exc:
            logger.debug("ROMsFun admin-ajax mint failed for %s: %s", mirror_url, exc)
        return None

    async def _resolve_cdn_url(self, client: httpx.AsyncClient, mirror_url: str) -> str | None:
        """Resolve a signed CDN URL using `client` so the token is minted in the
        SAME session/IP that will stream it (the v1.8.0 regression streamed from
        a fresh, cookie-less client → the anti-leech CDN 403'd). Order: visit the
        mirror page (sets cookies), mint via admin-ajax (primary), fall back to
        the page-embedded token, then to a FlareSolverr fetch if a solver is set
        and the direct page fetch was Cloudflare-blocked."""
        landing = re.sub(r"/\d+$", "", mirror_url)  # /download/{base}
        html = ""
        try:
            resp = await client.get(mirror_url, headers={**_HEADERS, "Referer": landing})
            if resp.status_code == 200:
                html = resp.text
        except Exception:
            html = ""

        cdn = await self._ajax_signed_url(client, mirror_url)
        if cdn:
            return cdn

        cdn = _parse_cdn_url(html) if html else None
        if cdn:
            return cdn

        if not html and self._flaresolverr_url:
            try:
                cdn = _parse_cdn_url(await self._fetch_html(mirror_url))
            except Exception:
                cdn = None
        return cdn

    async def _stream_to(self, client: httpx.AsyncClient, cdn_url: str, referer: str,
                         dest: Path, progress_callback) -> None:
        """Stream `cdn_url` to `dest` atomically (.part → os.replace). raise_for_status
        surfaces the anti-leech 403/429 as httpx.HTTPStatusError for the retry loop;
        a short body (bytes < Content-Length) is a truncated download, not a ROM."""
        tmp = dest.with_name(dest.name + ".part")
        try:
            async with client.stream(
                "GET", cdn_url, headers={**_HEADERS, "Referer": referer},
            ) as stream:
                stream.raise_for_status()
                total = int(stream.headers.get("content-length", 0))
                downloaded = 0
                with open(tmp, "wb") as fh:
                    async for chunk in stream.aiter_bytes(65536):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            await progress_callback(downloaded / total)
            if total and downloaded < total:
                raise SourceNetworkError(
                    f"short read {downloaded}/{total} bytes", source_id=self.source_id
                )
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    async def download_file(self, url: str, dest: Path, progress_callback=None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Be robust to a stored bare path (older queue entries / hunt candidates
        # stored the mirror path without a scheme → httpx "missing protocol").
        if url.startswith("/"):
            url = ROMSFUN_BASE + url
        elif not url.startswith("http"):
            url = f"{ROMSFUN_BASE}/{url.lstrip('/')}"

        landing = re.sub(r"/\d+$", "", url)  # /download/{base}
        last_exc: Exception | None = None

        # One client (cookie jar) per attempt: resolve the token AND stream it on
        # the same session, re-minting a fresh token each retry (the embedded /
        # ajax token can expire or be rate-limited).
        for attempt in range(_DOWNLOAD_ATTEMPTS):
            async with httpx.AsyncClient(
                headers={**_HEADERS, "Referer": landing},
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, read=300.0),
            ) as client:
                cdn_url = await self._resolve_cdn_url(client, url)
                if not cdn_url:
                    last_exc = SourceForbiddenError(
                        f"no signed CDN URL on mirror page {url}", source_id=self.source_id
                    )
                    await asyncio.sleep(2.0 * (2 ** attempt))
                    continue

                try:
                    await self._stream_to(client, cdn_url, url, dest, progress_callback)
                    return  # success
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    last_exc = classify_status(
                        code, source_id=self.source_id,
                        detail=f"ROMsFun CDN {code} for {cdn_url}",
                    )
                    if code not in (403, 429):
                        raise last_exc from exc
                except (httpx.TransportError, httpx.TimeoutException, SourceNetworkError) as exc:
                    last_exc = exc if isinstance(exc, SourceNetworkError) else SourceNetworkError(
                        f"ROMsFun CDN stream failed: {exc}", source_id=self.source_id
                    )

            if attempt < _DOWNLOAD_ATTEMPTS - 1:
                await asyncio.sleep(2.0 * (2 ** attempt))  # 2s, 4s backoff

        raise last_exc or SourceForbiddenError(
            f"ROMsFun download failed for {url}", source_id=self.source_id
        )


SOURCE_CLASS = RomsfunSource
