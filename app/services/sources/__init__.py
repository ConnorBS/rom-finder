"""ROM source registry — import this module to access all sources.

Sources are registered at import time. Use registry helpers to look
them up:
    from app.services.sources import registry
    source = registry.get("archive_org")
    results = await source.search("mario", "NES")
"""

from .registry import register, get, all_sources, enabled_sources
from .archive import ArchiveSource
from .vimm import VimmSource
from .romsfun import RomsfunSource
from .wowroms import WowromsSource

# Register all sources in display order
register(ArchiveSource())
register(VimmSource())
register(RomsfunSource())
register(WowromsSource())

__all__ = ["register", "get", "all_sources", "enabled_sources"]
