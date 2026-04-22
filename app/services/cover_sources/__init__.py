from .registry import register, all_sources, get
from .retroachievements import RetroAchievementsCoverSource
from .steamgriddb import SteamGridDBCoverSource

register(RetroAchievementsCoverSource())
register(SteamGridDBCoverSource())

__all__ = ["register", "all_sources", "get"]
