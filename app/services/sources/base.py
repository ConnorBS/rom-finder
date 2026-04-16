"""Abstract base class for ROM sources.

A source knows how to:
  - search for games/collections by title
  - list downloadable files within a result
  - produce a download URL
  - stream-download a file (default httpx impl; override for special cases)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import httpx


@dataclass
class SourceResult:
    """A collection or game returned from a source search."""
    identifier: str       # source-local key; passed back to get_files()
    title: str
    source_id: str
    description: str = ""


@dataclass
class SourceFile:
    """A downloadable ROM file within a result."""
    name: str
    identifier: str       # the parent result's identifier
    source_id: str
    size: int = 0
    md5: str = ""         # pre-known hash if the source provides it


class RomSource(ABC):
    source_id: str   # unique slug, e.g. "archive_org"
    name: str        # display name, e.g. "Internet Archive"
    available: bool = True  # False = stub, shown in UI but not selectable

    @abstractmethod
    async def search(self, query: str, system: str = "") -> list[dict]:
        """Search for games/collections. Returns list of dicts with at least
        'identifier', 'title', and optionally 'description'."""
        ...

    @abstractmethod
    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        """List downloadable files in a result. Returns list of dicts with at
        least 'name', 'identifier', and optionally 'size', 'md5'."""
        ...

    @abstractmethod
    def get_download_url(self, identifier: str, filename: str) -> str:
        """Return the direct download URL for a file."""
        ...

    def get_extra_headers(self) -> dict:
        """Optional headers to send with every download from this source."""
        return {}

    async def download_file(
        self,
        url: str,
        dest: Path,
        progress_callback=None,
    ) -> None:
        """Stream-download url to dest. Override for non-standard download flows."""
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
