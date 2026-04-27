# ROM Finder — Project Context

Paste this file at the start of any new AI session. It summarises where the project stands right now so the AI can pick up without re-reading the entire codebase.

---

## Current State (updated 2026-04-25)

The app is **production-ready and deployed** on Unraid at `192.168.0.100:19846`. All core workflows are functional.

### What's built and working

| Area | Status | Notes |
|------|--------|-------|
| Collection page | ✅ | Card + list view, filters, status badges, cover overlays |
| Wanted page | ✅ | RA search, add/remove games, source search per game |
| Search page | ✅ | Multi-source search (Archive.org, Vimm) |
| Downloads queue | ✅ | Progress tracking, hashing, RA verify, approve/reject |
| Settings | ✅ | All dirs, RA creds, sources, cover sources, folder map |
| Scheduler | ✅ | Library scan, hash check, RA autodiscover — all have Run Now |
| Logs page | ✅ | Structured activity log |
| Cover art | ✅ | RA + SteamGridDB sources, per-card refresh button |
| Sidebar nav | ✅ | Collection, Wanted, Search, Downloads, Settings, Scheduler, Logs |

### Recent changes (last few sessions)

- **QA pass** — Cover refresh UI (`POST /wanted/{id}/refresh-cover`, `POST /library/{id}/refresh-cover`); scheduler Run Now now shows loading spinner, updates Last Run timestamp via HTMX OOB swap; `/wanted` added to sidebar nav; `_build_collection()` deduped from 2 calls to 1.
- **Vimm source rewrite** — Fixed dead CDN (`download3` → `dl3.vimm.net`), switched GET→POST, reads actual `mediaId` from the vault page's `dl_form` hidden input (vault URL ID ≠ download mediaId), fixed name filter to use bidirectional stem comparison, added DMCA availability check, added "Open on Vimm" manual fallback. Vimm has JS bot protection that blocks automated POST — "Try download" may fail; users fall back to the manual link.
- **Review directory flag** — New `use_review_dir` setting (default on). When off: downloads go straight to ROMs dir and auto-create a LibraryEntry. Fixed readonly gate: `download_dir_readonly` no longer blocks downloads when review dir is enabled.

### Active decisions / constraints

- **SQLite sessions are synchronous** — never hold a session open across an `await`. Open a new `Session` after every async gap.
- **No JS framework** — All interactivity via HTMX 2. Minimal inline JS only for things HTMX can't do.
- **Migrations via `_MIGRATIONS` list in `main.py`** — Never Alembic. Add new columns here.
- **`_get_setting()` is intentionally duplicated per router** — consolidating would create circular imports.
- **Vimm requires JavaScript** — `dl3.vimm.net` returns 400 for automated requests. The code is correct; this is Vimm's bot protection. Only a headless browser could bypass it.

### Known gaps / next likely asks

- Tests: zero coverage today. Core targets: `hasher.py`, `title_utils.py`, `vimm.py` (name filter + URL logic), `collection.py` (_build_collection).
- Hash export (CSV/JSON of verified ROMs).
- RA progress tracking (achievement % per game).
- Import existing collection (bulk scan + hash + RA verify in one pass).
- Emulator launch from collection card.

---

## How to orient fast

| Question | Where to look |
|----------|---------------|
| Which file handles X endpoint? | `app/routers/` — one router per page/area |
| Where is a setting read? | `_get_setting(session, "key")` — duplicated per router intentionally |
| How do background tasks work? | `app/services/activity.py` + `FastAPI BackgroundTasks` |
| How does the scheduler work? | `app/services/scheduler.py` runs tasks; `app/routers/scheduler.py` has the UI |
| Where is the Vimm download logic? | `app/services/sources/vimm.py` — `download_file` override |
| Where are migrations? | `_MIGRATIONS` list in `app/main.py` |
