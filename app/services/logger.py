"""Structured activity logger.

Writes to the app_logs table so all search, download, hash, and error events
can be reviewed on the /logs page and exported for debugging.

Set verbose_logging = true in Settings to also capture every page navigation,
button press, and form submission (debug level).
"""

import json
from datetime import datetime

from app.db.models import AppLog


def _is_verbose() -> bool:
    """Return True when verbose_logging is enabled in app settings."""
    try:
        from app.db.database import engine
        from sqlmodel import Session
        from app.db.models import AppSetting
        with Session(engine) as session:
            s = session.get(AppSetting, "verbose_logging")
            return s is not None and s.value == "true"
    except Exception:
        return False


def log(
    level: str,
    category: str,
    message: str,
    details: dict | None = None,
) -> None:
    """Write a log entry synchronously. Safe to call from any context."""
    from app.db.database import engine
    from sqlmodel import Session

    try:
        entry = AppLog(
            ts=datetime.utcnow(),
            level=level,
            category=category,
            message=message,
            details=json.dumps(details or {}, default=str),
        )
        with Session(engine) as session:
            session.add(entry)
            session.commit()
    except Exception:
        pass  # never let logging crash the caller


def debug(category: str, message: str, details: dict | None = None) -> None:
    log("debug", category, message, details)


def info(category: str, message: str, details: dict | None = None) -> None:
    log("info", category, message, details)


def warning(category: str, message: str, details: dict | None = None) -> None:
    log("warning", category, message, details)


def error(category: str, message: str, details: dict | None = None) -> None:
    log("error", category, message, details)


def verbose(category: str, message: str, details: dict | None = None) -> None:
    """Write a debug-level entry only when verbose_logging is enabled."""
    if _is_verbose():
        log("debug", category, message, details)


# --- Convenience helpers ---

def log_search(
    source_name: str,
    query: str,
    system: str,
    result_count: int,
    error_msg: str = "",
) -> None:
    if error_msg:
        log("error", "search", f"{source_name}: \"{query}\" → error", {
            "source": source_name, "query": query, "system": system, "error": error_msg,
        })
    else:
        log("info", "search", f"{source_name}: \"{query}\" → {result_count} result(s)", {
            "source": source_name, "query": query, "system": system, "results": result_count,
        })


def log_download(
    game_title: str,
    file_name: str,
    source_url: str,
    status: str,
    error_msg: str = "",
) -> None:
    level = "error" if status == "failed" else "info"
    msg = f"Download {status}: {file_name}"
    log(level, "download", msg, {
        "game": game_title, "file": file_name,
        "url": source_url, "status": status, "error": error_msg,
    })


def log_hash(
    file_name: str,
    system: str,
    hash_value: str,
    hasher_used: str,
    ra_matched: bool,
    ra_game_id: int | None = None,
) -> None:
    matched_str = f"RA match: game {ra_game_id}" if ra_matched else "no RA match"
    log("info", "hash", f"Hash ({hasher_used}): {file_name} → {matched_str}", {
        "file": file_name, "system": system, "hash": hash_value,
        "hasher": hasher_used, "ra_matched": ra_matched, "ra_game_id": ra_game_id,
    })


def log_navigation(page: str, details: dict | None = None) -> None:
    """Log a page navigation event (verbose only)."""
    verbose("navigation", f"Page: {page}", details)


def log_action(action: str, details: dict | None = None) -> None:
    """Log a user action such as a button press or form submit."""
    log("info", "navigation", f"Action: {action}", details)


def log_action_verbose(action: str, details: dict | None = None) -> None:
    """Log a low-significance user action only when verbose mode is on."""
    verbose("navigation", f"Action: {action}", details)


def log_settings(message: str, details: dict | None = None) -> None:
    log("info", "settings", message, details)


def log_library(message: str, details: dict | None = None) -> None:
    log("info", "library", message, details)
