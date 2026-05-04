"""Base class for ROM download sources.

Each source implements three methods:

  search()          — find games matching a query; return one result per game
  get_files()       — given a search result, return the downloadable file(s)
  get_download_url()— convert a file identifier to a URL for download_file()

For most sources get_files() returns a single-item list (one file per game)
and that file's identifier is the direct CDN URL. The base download_file()
streams it with httpx. Override download_file() only when a fresh signed URL
must be fetched at download time (token-based CDNs like WowROMs/ROMsFun).

Archive.org is the exception — it returns collections that may contain many
files, so it exposes a "Browse files" step. New sources should not replicate
this; return individual games and one file per result instead.
"""

from abc import ABC, abstractmethod
from pathlib import Path

import httpx


class RomSource(ABC):
    source_id: str   # unique slug, e.g. "wowroms"
    name: str        # display name, e.g. "WowROMs"
    available: bool = True  # False = shown in UI but not selectable

    @abstractmethod
    async def search(self, query: str, system: str = "") -> list[dict]:
        """Search for games matching query. Return one dict per game with keys:
        identifier, title, source_id, description (optional).
        Filter by system slug when system is provided."""
        ...

    @abstractmethod
    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        """Fetch downloadable file(s) for a search result. Typically fetches
        the game page, extracts the CDN URL and real filename, and returns a
        single-item list. Keys: name, identifier (CDN URL or path for token
        refresh), source_id, size (bytes or 0)."""
        ...

    @abstractmethod
    def get_download_url(self, identifier: str, filename: str) -> str:
        """Convert a file identifier (from get_files) to the URL passed to
        download_file(). If the identifier is already a full URL, return it
        as-is. If it's a path fragment, prefix your base URL."""
        ...

    def get_extra_headers(self) -> dict:
        """Optional headers sent with every download request in the base
        download_file(). Override to add Referer, cookies, etc."""
        return {}

    async def download_file(self, url: str, dest: Path, progress_callback=None) -> None:
        """Stream url to dest. The base implementation uses httpx with
        follow_redirects and calls progress_callback(fraction) as data arrives.
        Override when the real CDN URL must be generated at download time
        (e.g. fetching a fresh signed token from a mirror page)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = self.get_extra_headers()
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=None, headers=headers
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            await progress_callback(downloaded / total)
