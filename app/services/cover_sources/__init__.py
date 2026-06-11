from .registry import register, unregister, all_sources, get
from .retroachievements import RetroAchievementsCoverSource
from .ra_v2_boxart import RAV2BoxArtCoverSource
from .steamgriddb import SteamGridDBCoverSource

register(RetroAchievementsCoverSource())
register(RAV2BoxArtCoverSource())
register(SteamGridDBCoverSource())

__all__ = ["register", "unregister", "all_sources", "get"]
