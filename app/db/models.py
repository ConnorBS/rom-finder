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
    moving = "moving"            # approved, file being moved to the ROMs dir


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
    # Set to the hunt's activity task id ("hunt-{wanted_id}") for a transient
    # Download row created by an auto-hunt attempt, so the card can offer a Cancel
    # that targets the hunt. None for a normal manual/queue download. (migration 0019)
    hunt_task_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HuntStatus(str, Enum):
    hunting = "hunting"
    verified = "verified"
    exhausted = "exhausted"   # all sources tried, no verified dump found
    awaiting_external = "awaiting_external"   # submitted to a torrent/usenet client; polled to completion


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


class GoalObjective(str, Enum):
    master = "master"    # satisfied when RAGameProgress.highest_award_kind == "mastered"
    beaten = "beaten"    # satisfied when highest_award_kind in ("beaten", "mastered") — hardcore only
    achievement = "achievement"  # satisfied when achievement_id is unlocked in hardcore (ra_achievement)
    custom = "custom"    # freeform objective (e.g. "finish level 5"); marked done by hand


class GoalStatus(str, Enum):
    active = "active"
    completed = "completed"
    failed = "failed"      # manually marked failed; hidden unless "show failed", red ✗ overlay


class Goal(SQLModel, table=True):
    """A user objective for a game, optionally grouped under an event, with a deadline.
    master/beaten goals auto-complete from the LOCAL RA mirror (ra_game_progress) via
    services/goals.py::evaluate_goals; custom goals are marked done manually. New table →
    created by SQLModel.metadata.create_all at startup; no migration needed."""
    __tablename__ = "goal"
    id: Optional[int] = Field(default=None, primary_key=True)
    game_title: str
    system: str = ""
    ra_game_id: Optional[int] = Field(default=None, index=True)  # None for custom/non-RA goals
    achievement_id: Optional[int] = Field(default=None, index=True)  # set for objective=achievement
    cover_path: str = ""             # "covers/{ra_game_id}.png" (reuses any cover already on disk)
    objective: str = GoalObjective.beaten
    custom_text: str = ""            # custom: freeform label ("finish level 5"); achievement: the achievement's title
    achievement_desc: str = ""       # achievement goals: the achievement's description (from the RA API)
    points: int = 0                  # achievement goals: the achievement's RA point value (event header sums these)
    event_name: str = Field(default="", index=True)   # "" = ungrouped
    category: str = ""               # sub-category within the event ("" = uncategorized); matches GoalCategory.name (migration 0026)
    deadline: Optional[datetime] = None   # midnight UTC of target day; None = no deadline
    status: str = GoalStatus.active
    auto: bool = False               # True once the RA evaluator (not the user) flipped it done
    # Custom card art (migration 0026): an uploaded image OVERRIDES the cover/badge; else
    # display_text (e.g. "+100 XP") renders large with `icon` (a tintable glyph) in
    # `icon_color` centered below. Priority: custom_image > display_text+icon > cover > letter.
    custom_image: str = ""           # relative path under static/, e.g. "covers/goal_42.png"
    display_text: str = ""           # text shown instead of an image
    icon: str = ""                   # one tintable glyph from GOAL_ICONS
    icon_color: str = ""             # hex colour applied to the glyph (e.g. "#fbbf24")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class GoalEvent(SQLModel, table=True):
    """A named event that groups goals. Either a CUSTOM event (user-made, with an
    optional URL — e.g. a Google Sheet — for quick navigation) or an RA-sourced event
    that auto-syncs new achievements nightly (AotW, random rolls, etc. grow over time).
    Goals join by matching Goal.event_name == GoalEvent.name. New table → created by
    create_all at startup; no migration needed. (Table is `goal_events` — plural — so
    its auto-named index ix_goal_events_name doesn't collide with goal.event_name's
    ix_goal_event_name.)"""
    __tablename__ = "goal_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)          # grouping key; matches Goal.event_name
    url: str = ""                          # optional link (Google Sheet / event page) for navigation
    ra_game_id: Optional[int] = Field(default=None, index=True)  # set for an RA event/game hub
    auto_sync: bool = False                # nightly re-check the RA hub for new achievements
    include_completed: bool = True         # remembered import option for nightly adds
    deadline: Optional[datetime] = None    # default deadline stamped on newly-synced achievement goals
    tiers_json: str = ""                   # JSON [{title,kind,points_required,badge_url}] award tiers (RA V2)
    last_synced_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GoalCategory(SQLModel, table=True):
    """A sub-category WITHIN an event — a titled, dated divider that goals are assigned to
    (Goal.category == GoalCategory.name, scoped by event_name). Categories and uncategorized
    games are ordered within the event by closest due date. New table → created by
    create_all at startup; no migration needed."""
    __tablename__ = "goal_categories"
    id: Optional[int] = Field(default=None, primary_key=True)
    event_name: str = Field(index=True)    # the parent event's name (matches Goal.event_name)
    name: str                              # the sub-category title (matches Goal.category)
    deadline: Optional[datetime] = None    # the sub-category's own target date; None = no deadline
    notes: str = ""                        # optional free text (light markdown) shown below the header; "" = no notes element
    # Optional RA game backing the sub-category (migration 0027): when set, the category
    # takes the game's identity — box art (cover_path), console (system), and a link to
    # retroachievements.org/game/{ra_game_id}. Attached by id or by manual search.
    ra_game_id: Optional[int] = Field(default=None, index=True)
    system: str = ""                       # the attached game's console (display name)
    cover_path: str = ""                   # "covers/{ra_game_id}.png" (box art) when a game is attached
    created_at: datetime = Field(default_factory=datetime.utcnow)
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


class ExternalDownload(SQLModel, table=True):
    """A torrent/usenet job submitted to a download client (qBittorrent/SABnzbd via
    Prowlarr) as a LAST-RESORT hunt fallback. Polled to completion by the scheduler
    `run_poll_external` task, which then ingests + RA-verifies the file. New table →
    created by create_all at startup; no migration needed (per db/CLAUDE.md)."""
    __tablename__ = "external_download"
    id: Optional[int] = Field(default=None, primary_key=True)
    wanted_game_id: int = Field(index=True)
    download_id: Optional[int] = None          # the linked Download row showing progress
    client_id: str = ""                        # download-client integration id
    protocol: str = ""                         # "torrent" | "usenet"
    job_handle: str = ""                       # qBit infohash or SAB nzo_id
    release_title: str = ""
    indexer: str = ""
    save_path: str = ""
    target_files: str = "[]"                   # JSON list of selected filenames (pack/multi-disc)
    # JSON {ra_stems, title_terms, accepted_md5s} captured at submit so the poller can
    # do file-selection + RA-hash verification with NO extra RA calls.
    match_data: str = "{}"
    needs_file_selection: bool = False         # torrent pack/multi-disc: trim files once metadata arrives
    # submitted | metadata | downloading | completed | verifying | verified | failed
    status: str = "submitted"
    progress: float = 0.0
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


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


class LibraryRoot(SQLModel, table=True):
    """A registered ROM directory. The library is the union of all roots (combined
    view). Exactly one root is `is_primary` — the download target. New table → created
    by SQLModel.metadata.create_all at startup; the primary is seeded from the legacy
    `download_dir` setting in lifespan (services/library_roots.ensure_primary_and_backfill)."""
    __tablename__ = "library_roots"
    id: Optional[int] = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)   # absolute dir path
    label: str = ""                              # display name ("Main", "Archive", "USB")
    is_primary: bool = False                     # exactly one; downloads/moves default here
    readonly: bool = False                       # scan/view only — never written/moved into or out of
    position: int = 0                            # display order
    # Per-directory folder->system map as JSON {folder_name: system}. NB direction:
    # folder->system (NOT the legacy global folder_map's system->folder) — the natural
    # shape for manual per-directory mapping + the scanner's direct lookup. Filing a
    # download inverts it (services/library_roots.dest_folder_for_system).
    folder_map: str = "{}"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryEntry(SQLModel, table=True):
    """ROMs that have been downloaded and are tracked locally."""
    __tablename__ = "library"
    id: Optional[int] = Field(default=None, primary_key=True)
    game_title: str
    system: str
    file_name: str
    file_path: str  # unique — ux_library_path (migration 0008); idempotent imports
    root_id: Optional[int] = Field(default=None, index=True)  # owning LibraryRoot (migration 0025);
                                                              # set on scan / download-approval / move
    file_hash: Optional[str] = None
    hash_verified: bool = False
    ra_game_id: Optional[int] = None
    ra_matched: bool = False
    cover_path: str = ""
    hashed_at: Optional[datetime] = None
    ra_checked_at: Optional[datetime] = None   # last RA hash lookup (migration 0009)
    missing: bool = False        # file gone from disk — flagged, not deleted (migration 0012)
    missing_at: Optional[datetime] = None       # when first detected missing
    duplicate_of: Optional[int] = None   # canonical sibling's library id when this entry is a
                                         # redundant copy (same content/game); None = canonical/unique (migration 0014)
    save_count: int = 0          # matched emulator save files; >0 = game has a save (migration 0015)
    save_files: str = ""         # JSON [{name,kind,size,mtime}] of matched saves — READ-ONLY,
                                 # the app never edits or deletes saves
    save_updated_at: Optional[datetime] = None   # newest matched save's mtime
    disc_id: str = ""            # 4-char Wii title-ID-low (e.g. "RKME") read from the disc
                                 # header — keys Dolphin NAND save matching (migration 0016)
    ra_award: str = ""           # own highest RA award tier (migration 0017): mastered | completed |
                                 # beaten | beaten-softcore | "" — derived from the RA dashboard mirror
    is_subset_rom: bool = False  # this entry is itself an RA "Subset" copy (title/filename marker)
    subset_info: str = ""        # JSON [{game_id,title,mastered}] of RA subsets whose accepted hash
                                 # list contains this ROM's file_hash — derived (read-only), RA-backed
    file_size: int = 0           # ROM file size in bytes (migration 0018); enables the size sort.
                                 # Populated/refreshed on scan + rehash (0 = not yet measured)
    time_to_beat_min: int = 0    # RA V2 medianTimeToBeatMinutes (migration 0023); 0 = unknown.
                                 # Captured by services/game_sets.refresh_game_sets (same V2 call as sets)
    chd_codec: str = ""          # CHD container compression status (migration 0024), set by
                                 # services/chd_format.run_chd_check: "" = not a CHD/unchecked,
                                 # "ok" = RA-safe codecs, "cdzs"/"zstd" = Zstandard (no cheevos in
                                 # RetroArch until re-encoded). Derived from the file header.
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


class RASubsetHash(SQLModel, table=True):
    """Cached RA subset → accepted-MD5 map. Refreshed by services/subsets.py::
    refresh_subset_cache (RA calls), which enumerates each owned game's subsets
    (from the per-console game list) and pulls their hash lists, replacing rows
    wholesale (full sweep) or per parent (scoped). A library ROM is matched to a
    subset purely by `md5 == LibraryEntry.file_hash`; `parent_game_id` only scopes
    which owned games' subsets to (re)fetch. Created by create_all (no migration)."""
    __tablename__ = "ra_subset_hash"
    id: Optional[int] = Field(default=None, primary_key=True)
    parent_game_id: int = Field(default=0, index=True)
    subset_game_id: int = Field(default=0, index=True)
    subset_title: str = ""
    console_id: int = 0
    md5: str = Field(default="", index=True)


class RAGameSet(SQLModel, table=True):
    """RA **V2** multiset awareness (alongside the V1 RASubsetHash model): an owned
    game's achievement SETS within its one game id, selected by `?set=`. Refreshed by
    services/game_sets.py::refresh_game_sets (one V2 /games/{id}?include=achievementSets,hashes
    call per owned game). `compatibility` = "compatible" (base ROM plays it) or
    "patch-required" (needs RA's compatibility patch — `patch_url`), derived from the set
    `set_type` (bonus/challenge = compatible; specialty/exclusive = patch-required) + the
    game's V2 hashes. Display-only (collection detail); created by create_all (no migration)."""
    __tablename__ = "ra_game_set"
    id: Optional[int] = Field(default=None, primary_key=True)
    game_id: int = Field(default=0, index=True)   # the owned game's RA id
    set_id: int = Field(default=0)                # achievement set id (= ?set=N)
    title: str = ""
    set_type: str = ""                            # core | bonus | challenge | specialty | exclusive
    compatibility: str = ""                       # compatible | patch-required
    patch_url: str = ""
    points_total: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)
