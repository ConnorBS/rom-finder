"""Internet Archive search and download client.

Uses the archive.org metadata and search APIs to find ROM files.
The Internet Archive hosts many No-Intro and Redump ROM collections,
which are the sets RetroAchievements primarily supports.
"""

import httpx
from pathlib import Path

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata"
ARCHIVE_DOWNLOAD_BASE = "https://archive.org/download"

# ROM file extensions to look for
ROM_EXTENSIONS = {
    ".zip", ".7z", ".rar",
    ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc",
    ".md", ".gen", ".smd", ".bin",
    ".iso", ".cue", ".chd",
    ".n64", ".z64", ".v64",
    ".nds", ".3ds",
    ".psp", ".cso",
}


class ArchiveClient:
    async def search_collections(self, query: str, system: str = "") -> list[dict]:
        """Search archive.org for ROM collections matching the query.

        Returns a list of archive.org item metadata dicts.
        """
        search_q = f'({query}) AND (subject:"No-Intro" OR subject:"Redump" OR subject:"ROM")'
        if system:
            search_q += f' AND (title:"{system}" OR description:"{system}")'

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                ARCHIVE_SEARCH_URL,
                params={
                    "q": search_q,
                    "fl[]": ["identifier", "title", "description", "subject"],
                    "rows": 25,
                    "page": 1,
                    "output": "json",
                },
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json().get("response", {}).get("docs", [])

    async def get_files(
        self,
        identifier: str,
        name_filter: str = "",
    ) -> list[dict]:
        """List files in an archive.org item, optionally filtered by name substring."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{ARCHIVE_METADATA_URL}/{identifier}",
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()

        files = data.get("files", [])

        # Keep only ROM-like files
        files = [
            f for f in files
            if Path(f.get("name", "")).suffix.lower() in ROM_EXTENSIONS
        ]

        if name_filter:
            name_filter_lower = name_filter.lower()
            files = [f for f in files if name_filter_lower in f.get("name", "").lower()]

        # Attach the identifier so callers can build download URLs
        for f in files:
            f["identifier"] = identifier

        return files

    def get_download_url(self, identifier: str, filename: str) -> str:
        return f"{ARCHIVE_DOWNLOAD_BASE}/{identifier}/{filename}"

    async def download_file(
        self,
        url: str,
        dest_path: Path,
        progress_callback=None,
    ) -> None:
        """Stream-download a file to dest_path with optional progress reporting.

        progress_callback(fraction: float) is called with values 0.0–1.0.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(dest_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            await progress_callback(downloaded / total)
