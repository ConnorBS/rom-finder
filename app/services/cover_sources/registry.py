from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import CoverSource

_registry: dict[str, "CoverSource"] = {}


def register(source: "CoverSource") -> None:
    _registry[source.source_id] = source


def unregister(source_id: str) -> None:
    _registry.pop(source_id, None)


def all_sources() -> list["CoverSource"]:
    return list(_registry.values())


def get(source_id: str) -> "CoverSource | None":
    return _registry.get(source_id)
