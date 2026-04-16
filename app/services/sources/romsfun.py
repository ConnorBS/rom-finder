"""romsfun.com ROM source (stub — not yet implemented).

romsfun.com does not provide a public API. A full implementation would
require scraping their search and download pages, which may change over
time. This stub is registered so the source appears in Settings; enable
it here once the scraper is implemented.
"""

from pathlib import Path
from .base import RomSource


class RomsfunSource(RomSource):
    source_id = "romsfun"
    name = "ROMsFun"
    available = False   # shown as unavailable in the UI

    async def search(self, query: str, system: str = "") -> list[dict]:
        return []

    async def get_files(self, identifier: str, name_filter: str = "") -> list[dict]:
        return []

    def get_download_url(self, identifier: str, filename: str) -> str:
        return ""
