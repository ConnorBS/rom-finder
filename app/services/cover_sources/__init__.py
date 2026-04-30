from .registry import register, unregister, all_sources, get
from .retroachievements import RetroAchievementsCoverSource
from .steamgriddb import SteamGridDBCoverSource

register(RetroAchievementsCoverSource())
register(SteamGridDBCoverSource())

__all__ = ["register", "unregister", "all_sources", "get"]
