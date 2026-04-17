"""Internet Archive (archive.org) ROM source.

Uses the archive.org metadata and search APIs to find ROM files.
Migrated from app/services/archive_client.py.
"""

import httpx
from pathlib import Path

from .base import RomSource

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata"
ARCHIVE_DOWNLOAD_BASE = "https://archive.org/download"

ROM_EXTENSIONS = {
    ".zip", ".7z", ".rar",
    ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc",
    ".md", ".gen", ".smd", ".bin",
    ".iso", ".cue", ".chd",
    ".n64", ".z64", ".v64",
    ".nds", ".3ds",
    ".psp", ".cso",
}


class ArchiveSource(RomSource):
    source_id = "archive_org"
    name = "Internet Archive"

    async def search(self, query: str, system: str = "") -> list[dict]:
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
            results = resp.json().get("response", {}).get("docs", [])

        for r in results:
            r["source_id"] = self.source_id
        return results

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{ARCHIVE_METADATA_URL}/{identifier}",
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()

        files = data.get("files", [])
        files = [
            f for f in files
            if Path(f.get("name", "")).suffix.lower() in ROM_EXTENSIONS
        ]

        if name_filter:
            # Strip extension before matching: RA ROM names end in .iso/.bin but
            # Archive files are often .7z or .zip with the same base name.
            filter_stem = Path(name_filter).stem.lower()
            files = [f for f in files if filter_stem in Path(f.get("name", "")).stem.lower()]

        for f in files:
            f["identifier"] = identifier
            f["source_id"] = self.source_id

        return files

    def get_download_url(self, identifier: str, filename: str) -> str:
        return f"{ARCHIVE_DOWNLOAD_BASE}/{identifier}/{filename}"
