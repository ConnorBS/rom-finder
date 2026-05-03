"""ROM source registry — import this module to access all sources.

Built-in sources are registered at import time. Extension sources are
registered at startup by extension_loader.py.
"""

from .registry import register, unregister, get, all_sources, enabled_sources
from .archive import ArchiveSource
from .cdromance import CdromanceSource

register(ArchiveSource())
register(CdromanceSource())

__all__ = ["register", "unregister", "get", "all_sources", "enabled_sources"]
