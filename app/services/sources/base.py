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

import os
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from app.services.sources.errors import (
    SourceError, SourceNetworkError, classify_status,
)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # HTTP-date form not handled; seconds form is what CDNs/RA use


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
        """Stream url to dest. Streams to a `.part` temp file and atomically
        renames on success, so a failed/partial download never leaves a file the
        scanner would mistake for a ROM. Verifies the byte count against
        Content-Length when present, and raises a typed SourceError on HTTP
        failure or a short/incomplete body. Override when the real CDN URL must
        be generated at download time (e.g. fetching a fresh signed token)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = self.get_extra_headers()
        tmp = dest.with_name(dest.name + ".part")
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=None)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout, headers=headers
            ) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        raise classify_status(
                            resp.status_code,
                            source_id=getattr(self, "source_id", ""),
                            retry_after=_parse_retry_after(resp.headers.get("retry-after")),
                            detail=f"{self.name}: HTTP {resp.status_code} for {url}",
                        )
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with open(tmp, "wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total:
                                await progress_callback(downloaded / total)
            if total and downloaded != total:
                raise SourceNetworkError(
                    f"{self.name}: incomplete download ({downloaded}/{total} bytes) for {url}",
                    source_id=getattr(self, "source_id", ""),
                )
            os.replace(tmp, dest)  # atomic on the same filesystem
        except SourceError:
            tmp.unlink(missing_ok=True)
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            tmp.unlink(missing_ok=True)
            raise SourceNetworkError(
                f"{self.name}: {type(exc).__name__} for {url}",
                source_id=getattr(self, "source_id", ""),
            ) from exc
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
