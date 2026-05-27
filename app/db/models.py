from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class DownloadStatus(str, Enum):
    pending = "pending"
    downloading = "downloading"
    hashing = "hashing"          # download done, computing hash
    verifying = "verifying"      # hash done, querying RetroAchievements
    completed = "completed"
    failed = "failed"
    verified = "verified"
    pending_approval = "pending_approval"


class AppSetting(SQLModel, table=True):
    """Key/value store for user-configurable settings."""
    __tablename__ = "app_settings"
    key: str = Field(primary_key=True)
    value: str = ""


class Download(SQLModel, table=True):
    """Tracks items in the download queue."""
    id: Optional[int] = Field(default=None, primary_key=True)
    game_title: str
    system: str
    file_name: str
    file_path: Optional[str] = None  # unique when non-null — ux_download_path (migration 0007)
    source_url: str
    source_id: str = "archive_org"
    archive_identifier: str = ""
    status: DownloadStatus = DownloadStatus.pending
    progress: float = 0.0
    file_hash: Optional[str] = None
    hash_verified: bool = False
    ra_game_id: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HuntStatus(str, Enum):
    hunting = "hunting"
    verified = "verified"
    exhausted = "exhausted"   # all sources tried, no verified dump found


class WantedGame(SQLModel, table=True):
    """A game the user is actively hunting for."""
    __tablename__ = "wanted_games"
    id: Optional[int] = Field(default=None, primary_key=True)
    game_title: str
    system: str
    ra_game_id: int = Field(index=True)  # (ra_game_id, system) unique — ux_wanted_ra_system (migration 0006)
    cover_path: str = ""        # relative path under static/, e.g. "covers/1234.png"
    status: HuntStatus = HuntStatus.hunting
    last_hunt_at: Optional[datetime] = None   # when auto-hunt last ran
    added_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HuntAttempt(SQLModel, table=True):
    """Records each auto-hunt download attempt so failures are not retried."""
    __tablename__ = "hunt_attempts"
    id: Optional[int] = Field(default=None, primary_key=True)
    wanted_game_id: int = Field(index=True)
    source_id: str = ""
    identifier: str = ""      # source-local key (vault ID, archive ID, etc.)
    file_name: str = ""
    source_url: str = ""      # resolved download URL — stable per-file id for dedup + audit
    file_hash: Optional[str] = None
    result: str = ""          # "bad_hash" | "download_failed" | "verified"
    tried_at: datetime = Field(default_factory=datetime.utcnow)


class LogLevel(str, Enum):
    debug = "debug"
    info = "info"
    warning = "warning"
    error = "error"


class LogCategory(str, Enum):
    search = "search"
    download = "download"
    hash = "hash"
    navigation = "navigation"
    source = "source"
    system = "system"
    settings = "settings"
    library = "library"


class AppLog(SQLModel, table=True):
    """Structured activity log for debugging and audit."""
    __tablename__ = "app_logs"
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow, index=True)
    level: str = LogLevel.info
    category: str = ""
    message: str = ""
    details: str = "{}"   # JSON payload


class InstalledExtension(SQLModel, table=True):
    """An extension installed from an external repository."""
    __tablename__ = "installed_extensions"
    id: Optional[int] = Field(default=None, primary_key=True)
    ext_id: str = Field(index=True, unique=True)
    name: str
    version: str = "0.0.0"
    ext_type: str = "rom_source"   # "rom_source" | "cover_source"
    author: str = ""
    description: str = ""
    pkg_url: str = ""
    file_name: str = ""
    enabled: bool = True
    installed_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryEntry(SQLModel, table=True):
    """ROMs that have been downloaded and are tracked locally."""
    __tablename__ = "library"
    id: Optional[int] = Field(default=None, primary_key=True)
    game_title: str
    system: str
    file_name: str
    file_path: str  # unique — ux_library_path (migration 0008); idempotent imports
    file_hash: Optional[str] = None
    hash_verified: bool = False
    ra_game_id: Optional[int] = None
    ra_matched: bool = False
    cover_path: str = ""
    hashed_at: Optional[datetime] = None
    ra_checked_at: Optional[datetime] = None   # last RA hash lookup (migration 0009)
    missing: bool = False        # file gone from disk — flagged, not deleted (migration 0012)
    missing_at: Optional[datetime] = None       # when first detected missing
    added_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# RetroAchievements dashboard — a LOCAL mirror of the configured user's RA data.
# A manual refresh (services/ra_dashboard.py) REPLACES these tables wholesale, so
# retroactive RA changes (repointed/removed/demoted achievements, backdated
# unlocks) reconcile cleanly — the mirror is never append-only. Created by
# SQLModel.metadata.create_all at startup (no migration needed for new tables).
# ---------------------------------------------------------------------------

class RAAchievement(SQLModel, table=True):
    """One earned achievement unlock (from API_GetAchievementsEarnedBetween).
    The backbone the dashboard's metrics/graphs/search/time-filters derive from."""
    __tablename__ = "ra_achievement"
    id: Optional[int] = Field(default=None, primary_key=True)
    achievement_id: int = Field(index=True)
    title: str = ""
    description: str = ""
    points: int = 0
    true_ratio: int = 0          # RA "TrueRatio" — rarity-weighted points
    type: str = ""               # progression | win_condition | missable | ""
    game_id: int = Field(default=0, index=True)
    game_title: str = ""
    console_id: int = Field(default=0, index=True)
    console_name: str = ""
    badge_url: str = ""
    earned_at: datetime = Field(index=True)
    hardcore: bool = False


class RAGameProgress(SQLModel, table=True):
    """Per-game completion for the configured user (API_GetUserCompletionProgress).
    `owned` is True when game_id matches a LibraryEntry.ra_game_id — the cross-link
    to the owned ROM library."""
    __tablename__ = "ra_game_progress"
    id: Optional[int] = Field(default=None, primary_key=True)
    game_id: int = Field(index=True, unique=True)
    title: str = ""
    console_id: int = Field(default=0, index=True)
    console_name: str = ""
    image_icon: str = ""
    max_possible: int = 0
    num_awarded: int = 0
    num_awarded_hardcore: int = 0
    pct_complete: float = 0.0
    highest_award_kind: str = ""    # mastered | completed | beaten | beaten-softcore | ""
    highest_award_date: Optional[datetime] = None
    most_recent_date: Optional[datetime] = None
    owned: bool = False


class RAProfile(SQLModel, table=True):
    """Snapshot of the configured user's RA profile/headline stats. Single row (id=1)."""
    __tablename__ = "ra_profile"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = ""
    points: int = 0                 # hardcore points
    points_softcore: int = 0
    rank: int = 0
    total_achievements: int = 0     # hardcore unlocks in the mirror
    total_games: int = 0            # games with >=1 achievement earned
    total_masteries: int = 0
    member_since: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
