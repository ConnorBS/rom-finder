# ROM Finder — Project Guide

## What This Project Is

A self-hosted web app for managing a personal ROM collection with a focus on **RetroAchievements (RA) hash verification**. Core workflow:

1. **Discover** — search RA's game database; add games to the Wanted pool
2. **Find** — search ROM sources (Archive.org, Vimm, etc.) and download
3. **Verify** — hash each ROM and match against RA's accepted hash list
4. **Track** — maintain a library of verified ROMs with cover art

Every ROM kept must be a hash-verified, RA-accepted dump — not just any ROM that runs.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, FastAPI (async) |
| Frontend | Jinja2 templates, HTMX 2, Tailwind CSS (CDN) |
| Database | SQLite via SQLModel (sync sessions, async routes) |
| Deploy | Docker on Unraid, GitHub Actions CI/CD → webhook triggers pull+restart |
| HTTP client | httpx (async) |

**No JS framework.** All interactivity is HTMX. JS in templates is minimal — only for things HTMX can't do (dropdowns, card overlay polling, drag-to-reorder).

---

## Deployment

- Source: `https://github.com/ConnorBS/rom-finder`
- Docker image built by GitHub Actions on push to `main`
- Unraid at `192.168.0.100`; app on port `19846`
- Appdata (DB + covers): `/mnt/ssd_cache/appdata/rom-finder/`
- Deploy verification: check `rom-finder-webhook` container logs or image revision label

---

## Workflow Rules

- **After every change**: update the relevant CLAUDE.md(s), then commit and push to `main`
- **Quality bar**: all edge cases handled in the first push — no follow-up PRs for obvious gaps
- **No Alembic**: schema changes go through `_MIGRATIONS` in `main.py` only

---

## Adding a New Feature — Checklist

1. **New DB column?** Add to `models.py` + entry in `_MIGRATIONS` in `main.py`
2. **New setting?** Add to `DEFAULT_SETTINGS` in `main.py` — seeded automatically at startup
3. **New router?** Register in `main.py::app.include_router(...)`. Add nav link in `base.html` if user-facing
4. **New background task?** Use `activity_store.start_batch(..., entry_ids=[...])` for per-card overlays
5. **New cover source?** Subclass `BaseCoverSource` → `app/services/cover_sources/` → register in `registry.py` → add settings to `DEFAULT_SETTINGS`
6. **New ROM source?** Same pattern under `app/services/sources/`

---

## Future Work

- **Hash export**: CSV/JSON of verified ROMs for emulator frontends
- **Emulator integration**: Launch a game from the collection view
- **More ROM sources**: Vimm, ROMsFun, WowROMs (stubs exist)
- **RA progress tracking**: Achievement completion % per game
- **Bulk RA verify after hash**: Auto-verify newly-hashed entries
- **Import from existing collection**: Bulk-import + hash + match in one pass
- **Notification on autodiscover**: Alert when Wanted pool grows via scheduler
