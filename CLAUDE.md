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
| Headless browser | Playwright (Chromium) — bundled in the Docker image for the Vimm extension's JS-challenge bypass |

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
- **No Alembic**: schema changes go through the ordered, tracked registry in `app/db/migrations.py`

---

## Verifying a Change (agent-driven, no human site checks)

Confirm a change works by pulling machine-readable feedback from the app — never by asking the user to open the site.

- **Fast local run** (no Docker daemon needed): `scripts/run-local.ps1` boots the app from the test venv (`./venv`) on a throwaway DB and prints `/api/status`. Or run `pytest` against the `tests/conftest.py` fixtures (throwaway-DB engine + `TestClient`).
- **Full-image run**: `docker compose -f docker-compose.local.yml up --build -d`, then `curl /api/status` and `curl "/api/logs?level=error"`, then `down -v`.
- **`GET /api/status`** reports version, RAHasher availability, DB counts (incl. `no_ra`), scheduler last-runs, verify progress, sources/extensions, and recent errors. **`GET /api/logs`** is the JSON log feed. These are the source of truth for "did it work?" — the `deploy-verifier` and `prod-health-monitor` agents read them.
- After deploy, confirm `/api/status.version` == the pushed git SHA (also shown on the Settings page).

---

## Adding a New Feature — Checklist

1. **New DB column?** Add to `models.py` + a new `(version_id, apply_fn)` entry in `app/db/migrations.py`
2. **New setting?** Add to `DEFAULT_SETTINGS` in `app/services/settings.py` — seeded automatically at startup. Read it via `app.services.settings` (`get`/`get_bool`/`get_json`)
3. **New router?** Register in `main.py::app.include_router(...)`. Add nav link in `base.html` if user-facing
4. **New background task?** Use `activity_store.start_batch(..., entry_ids=[...])` for per-card overlays
5. **New cover source?** Subclass `CoverSource` → `app/services/cover_sources/` → register in `registry.py` → add settings to `DEFAULT_SETTINGS`
6. **New ROM source?** Same pattern under `app/services/sources/`
7. **New extension?** Create a `.py` in `extensions/` with `EXTENSION_INFO` + `EXTENSION_SETTINGS` + `SOURCE_CLASS` / `COVER_SOURCE_CLASS`. Add entry to `extensions/index.json`. Extensions install/remove/toggle/update at `/extensions`. Extension settings appear on the Settings page automatically. Settings stored as `ext_{ext_id}_{key}` in AppSetting; passed to `configure(config)` if the source defines it.

---

## Future Work

- **Hash export**: CSV/JSON of verified ROMs for emulator frontends
- **Emulator integration**: Launch a game from the collection view
- **More ROM sources**: Vimm, ROMsFun, WowROMs, CDRomance are now extensions in `extensions/` — install via `/extensions`
- **RA progress tracking**: Achievement completion % per game
- **Bulk RA verify after hash**: Auto-verify newly-hashed entries
- **Import from existing collection**: Bulk-import + hash + match in one pass
- **Notification on autodiscover**: Alert when Wanted pool grows via scheduler
