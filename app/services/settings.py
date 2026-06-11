"""Settings access — the single source of truth for reading/writing AppSetting.

This is a LEAF module: it imports ONLY from `app.db` and the stdlib, never from
routers or other services. That's deliberate — the old per-router `_get_setting`
duplication existed to avoid import cycles between routers; a leaf module that
depends only on the data layer has no such cycle, so it's the right home for the
one canonical implementation.

Typed getters coerce the string-valued store and, crucially, `get_json` never
raises on malformed JSON (a bad `folder_map` used to 500 whole endpoints).
"""

import json
import os
from pathlib import Path
from typing import Any

from sqlmodel import Session

from app.db.models import AppSetting


# --- Default settings (seeded at startup in main.py lifespan) --------------

DEFAULT_SETTINGS: dict[str, str] = {
    "download_dir": os.environ.get("DOWNLOAD_DIR", str(Path.home() / "ROMs")),
    "check_dir": os.environ.get("CHECK_DIR", str(Path.home() / "ROMs-check")),
    "covers_dir": os.environ.get("COVERS_DIR", "static/covers"),
    "folder_map": "{}",
    "ra_enabled": "false",
    "ra_username": "",
    "ra_api_key": "",
    # Source enabled flags
    "source_archive_org_enabled": "true",
    # Extension system
    "extensions_dir": os.environ.get("EXTENSIONS_DIR", "extensions"),
    "extension_repos": '["https://raw.githubusercontent.com/ConnorBS/rom-finder/main/extensions/index.json"]',
    # Verbose logging captures every page load, button press, and navigation event
    "verbose_logging": "false",
    # /api/status recent_errors window
    "diagnostics_recent_hours": "24",
    # When true, downloads stage in check_dir for manual review before moving to download_dir.
    "use_review_dir": "true",
    # Emulator save-file directory (e.g. RetroAchievements/RetroArch savefile dir). READ-ONLY:
    # the app only scans it to flag which games have saves — it never edits or deletes saves.
    # Saves alongside ROMs (download_dir/check_dir) are also detected, so this is optional.
    "saves_dir": "",
    # Per-directory read-only locks — prevent any edits, deletes, or writes within the app
    "download_dir_readonly": "false",
    "check_dir_readonly": "false",
    "covers_dir_readonly": "false",
    # Cover art sources — RA on by default, SteamGridDB opt-in
    "cover_sources_order": '["retroachievements", "ra_v2_boxart", "steamgriddb"]',
    "cover_source_retroachievements_enabled": "true",
    "cover_source_ra_v2_boxart_enabled": "false",
    "cover_source_steamgriddb_enabled": "false",
    "cover_source_steamgriddb_api_key": "",
    # Autodiscover — periodically add newly-released RA games to Wanted pool
    "ra_autodiscover_enabled": "false",
    "ra_autodiscover_last_checked": "",
    # Scheduler — daily task schedule (local time HH:MM)
    "sched_scan_enabled": "true",
    "sched_scan_time": "04:00",
    "sched_scan_last_run": "",
    "sched_hash_enabled": "true",
    "sched_hash_time": "04:00",
    "sched_hash_last_run": "",
    "sched_autodiscover_enabled": "true",
    "sched_autodiscover_time": "04:00",
    "sched_autodiscover_last_run": "",
    # Resilient bulk RA re-verify (Phase 5)
    "sched_verify_enabled": "true",
    "sched_verify_time": "05:00",
    "sched_verify_last_run": "",
    # Nightly event sync — re-check auto-sync RA events for newly-added achievements
    "sched_eventsync_enabled": "true",
    "sched_eventsync_time": "05:30",
    "sched_eventsync_last_run": "",
    # Auto-set imported event goals' deadline from the event's end date (RA V2
    # `activeThrough`) when no deadline is given. Best-effort; needs V2 reachable.
    "event_pull_deadline": "true",
    # Torrent/usenet last-resort downloads (qBittorrent/SABnzbd via Prowlarr). The
    # poller runs every scheduler tick while jobs are in flight (not a daily slot).
    "sched_poll_external_enabled": "true",
    "external_download_stall_minutes": "120",
    "ra_verify_in_progress": "false",
    "ra_verify_paused_until": "",
    "ra_verify_last_run": "",
    "ra_verify_batch_size": "500",
    # RetroAchievements dashboard — local mirror of the user's earned achievements.
    # Manual refresh only (full re-pull + replace, to absorb retroactive RA changes).
    "ra_dashboard_last_sync": "",
    "ra_dashboard_window_days": "60",
    # Hash-aware subset cache (services/subsets.py) — last RA-backed refresh.
    "subset_cache_last_sync": "",
}


# --- Accessors -------------------------------------------------------------

def get(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def get_bool(session: Session, key: str, default: bool = False) -> bool:
    s = session.get(AppSetting, key)
    if s is None:
        return default
    return s.value == "true"


def get_json(session: Session, key: str, default: Any) -> Any:
    """Parse a JSON-valued setting. Returns `default` (not an exception) on a
    missing key OR malformed JSON — a corrupted setting must never 500 a page."""
    s = session.get(AppSetting, key)
    if s is None or s.value == "":
        return default
    try:
        return json.loads(s.value)
    except (json.JSONDecodeError, TypeError):
        return default


def set(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key)
    if s is None:
        session.add(AppSetting(key=key, value=value))
    else:
        s.value = value
        session.add(s)
    session.commit()


def get_extension_config(session: Session, ext_id: str) -> dict[str, str]:
    """All `ext_{ext_id}_{name}` settings as `{name: value}` — the config dict
    passed to an extension's configure()."""
    from sqlmodel import select
    prefix = f"ext_{ext_id}_"
    rows = session.exec(select(AppSetting)).all()
    return {r.key[len(prefix):]: r.value for r in rows if r.key.startswith(prefix)}
