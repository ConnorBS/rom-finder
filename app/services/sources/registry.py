"""Source registry — singleton dict of all registered RomSource instances."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import RomSource

_registry: dict[str, "RomSource"] = {}


def register(source: "RomSource") -> None:
    _registry[source.source_id] = source


def unregister(source_id: str) -> None:
    _registry.pop(source_id, None)


def get(source_id: str) -> "RomSource | None":
    return _registry.get(source_id)


def all_sources() -> list["RomSource"]:
    return list(_registry.values())


def enabled_sources(enabled_ids: set[str]) -> list["RomSource"]:
    return [s for s in _registry.values() if s.source_id in enabled_ids]
