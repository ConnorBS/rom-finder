"""Resilient bulk RA re-verify.

Why this exists: a bulk verify on 2026-05-03 hit RA's 429 rate-limit and was
never retried, leaving ~2789 hashed ROMs without an RA match. The old loop held
one Session open across every `await lookup_hash` (SQLite lock risk) and, on a
second 429, just kept hammering at 2 req/s.

This driver instead:
  - derives the work set from the DB each pass (repository.library_pending_ra_check),
    so a crash/restart simply recomputes the remainder — no in-memory state to lose;
  - opens a FRESH Session per entry (never held across the lookup await);
  - on a 429 (SourceRateLimitError), persists an escalating global pause
    (ra_verify_paused_until) and stops the pass — resume happens automatically on
    the next scheduler tick / manual run, honouring the pause even across restarts;
  - stamps ra_checked_at so genuine misses aren't re-checked until they go stale.
"""

import asyncio
from datetime import datetime, timedelta

from sqlmodel import Session

from app.db.database import engine
from app.db import repository
from app.db.models import LibraryEntry
from app.services import settings as app_settings
from app.services import logger as applog
from app.services import activity as activity_store
from app.services.ra_client import RAClient, RA_UNSUPPORTED_SYSTEMS
from app.services.sources.errors import SourceRateLimitError

_BACKOFF_STEPS = [60, 120, 300, 600]  # seconds — escalate on repeated 429


def _is_paused() -> str:
    with Session(engine) as s:
        until = app_settings.get(s, "ra_verify_paused_until", "")
    if not until:
        return ""
    try:
        return until if datetime.utcnow() < datetime.fromisoformat(until) else ""
    except ValueError:
        return ""


async def run_pass(max_entries: int | None = None, stale_days: int = 7) -> dict:
    """Run one resumable verify pass. Returns a summary dict."""
    paused = _is_paused()
    if paused:
        return {"status": "paused", "paused_until": paused, "checked": 0}

    with Session(engine) as s:
        username = app_settings.get(s, "ra_username")
        api_key = app_settings.get(s, "ra_api_key")
        if not (username and api_key):
            return {"status": "no_credentials", "checked": 0}
        if max_entries is None:
            try:
                max_entries = int(app_settings.get(s, "ra_verify_batch_size", "500") or "500")
            except ValueError:
                max_entries = 500
        work = repository.library_pending_ra_check(s, stale_days=stale_days, limit=max_entries,
                                                    exclude_systems=RA_UNSUPPORTED_SYSTEMS)
        snapshot = [(e.id, e.file_hash) for e in work]

    total = len(snapshot)
    if total == 0:
        with Session(engine) as s:
            app_settings.set(s, "ra_verify_last_run", datetime.utcnow().isoformat())
            app_settings.set(s, "ra_verify_paused_until", "")
        return {"status": "clear", "checked": 0, "matched": 0, "remaining": 0}

    ra = RAClient(username, api_key)
    entry_ids = {f"lib-{eid}" for eid, _ in snapshot}
    activity_store.start_batch("ra-verify-batch", "RA re-verify", total, "verify", entry_ids=entry_ids)
    with Session(engine) as s:
        app_settings.set(s, "ra_verify_in_progress", "true")

    checked = matched = 0
    hit_rate_limit = False
    try:
        for eid, file_hash in snapshot:
            if not file_hash:
                activity_store.increment("ra-verify-batch")
                continue
            try:
                result = await ra.lookup_hash(file_hash)
            except SourceRateLimitError as exc:
                wait = exc.retry_after or _BACKOFF_STEPS[min(checked // 50, len(_BACKOFF_STEPS) - 1)]
                until = (datetime.utcnow() + timedelta(seconds=wait)).isoformat()
                with Session(engine) as s:
                    app_settings.set(s, "ra_verify_paused_until", until)
                applog.warning("hash", f"RA verify hit 429 — pausing {int(wait)}s (resumable)",
                               {"checked": checked, "remaining": total - checked})
                hit_rate_limit = True
                break
            except Exception as exc:
                applog.warning("hash", f"RA verify lookup error: {exc}", {"entry_id": eid})
                activity_store.increment("ra-verify-batch")
                continue

            matched_id = result.get("ID") if result else None
            with Session(engine) as s:
                e = s.get(LibraryEntry, eid)
                if e:
                    e.ra_checked_at = datetime.utcnow()
                    e.hash_verified = True
                    if matched_id:
                        e.ra_matched = True
                        e.ra_game_id = matched_id
                    s.add(e)
                    if matched_id:
                        repository.mark_wanted_verified(s, matched_id)
                    s.commit()
            if matched_id:
                matched += 1
            checked += 1
            activity_store.increment("ra-verify-batch")

        if not hit_rate_limit:
            with Session(engine) as s:
                app_settings.set(s, "ra_verify_paused_until", "")
    finally:
        activity_store.finish("ra-verify-batch")
        with Session(engine) as s:
            app_settings.set(s, "ra_verify_in_progress", "false")
            app_settings.set(s, "ra_verify_last_run", datetime.utcnow().isoformat())

    with Session(engine) as s:
        remaining = len(repository.library_pending_ra_check(s, stale_days=stale_days,
                                                             exclude_systems=RA_UNSUPPORTED_SYSTEMS))
    applog.info("hash", f"RA re-verify pass: checked {checked}, matched {matched}, {remaining} pending",
                {"checked": checked, "matched": matched, "remaining": remaining, "rate_limited": hit_rate_limit})
    return {"status": "rate_limited" if hit_rate_limit else "ok",
            "checked": checked, "matched": matched, "remaining": remaining}
