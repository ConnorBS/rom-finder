from abc import ABC, abstractmethod


class CoverSource(ABC):
    source_id: str
    name: str
    description: str
    requires_api_key: bool = False

    @abstractmethod
    async def fetch_cover(
        self,
        ra_game_id: int,
        game_title: str,
        system: str,
        config: dict,
    ) -> bytes | None:
        """Return image bytes or None if not found / credentials missing."""
        ...
