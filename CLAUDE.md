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

## Done (recent stabilization/modularization pass)

- **Hash export** ✅ — `/export/hashes?format=csv|json` (links on Settings)
- **Bulk import** ✅ — the scheduler **Library scan** task (`/scheduler` → Run now, or daily) walks `download_dir`, imports untracked ROMs, hashes (RAHasher-first + disc guard), fetches covers, and RA-verifies — that IS the one-pass importer
- **Bulk RA verify after hash** ✅ — resumable, rate-limit-aware **RA re-verify** scheduler task (`ra_verify.run_pass`)
- **RetroAchievements Dashboard** ✅ — `/dashboard` (Overview / Timeline / Games / Insights / Reports). A **local mirror** of the configured user's RA data (`ra_achievement`/`ra_game_progress`/`ra_profile`), so all metrics/graphs/search/time-filters run with **zero RA calls**. Synced by a **manual** Refresh (`ra_dashboard.refresh()`) that **fully re-pulls + replaces** the mirror — that's how retroactively-changed achievements reconcile. Charts via ApexCharts (CDN); games cross-referenced to the owned library by RA ID; reports export as RetroAchievements forum markup (`ra_report.py`). State surfaces in `/api/status.dashboard`.
- **Award badges + hash-aware subsets + multi-select** ✅ — collection shows RA award tier (gold **🏅 Mastered** / **✅ Completed** / **🏆 Beaten**; Mastered supersedes) from `LibraryEntry.ra_award` (derived by `services/mastery.py::sync_library_awards` off the local mirror — LOCAL, hardcore-only). Subsets are **hash-aware**: `services/subsets.py::refresh_subset_cache` (the only RA-calling part) enumerates each owned game's subsets + their accepted hashes into `ra_subset_hash`, and `recompute_subset_flags` (LOCAL) marks each ROM `is_subset_rom` + `subset_info` (compatible subsets, each mastered/not). Subset discovery rides the recurring RA passes (autodiscover full sweep + Check-RA/verify scoped) + a manual **Detect subsets** button. Filters: `has_mastered` / `beaten` / `subset_available`. **Multi-select**: per-card/row checkboxes, page-size up to 1000, Select-all-filtered, and a selection bar that runs Hash / Fetch covers / Verify / **Delete** (`/collection/bulk/delete`) on the selection (bulk endpoints read `library_ids` from the request body via `Form`/`hx-vals`). **Sort** (`?sort=`): size (`LibraryEntry.file_size`, migration 0018, set on scan/rehash), date added, progress / achievements / points (RA mirror), title, system. **Delete also clears the matching Wanted record** (`_purge_orphaned_wanted`) — but only once no duplicate/subset copy remains — so deleted games leave the collection. Counts in `/api/status.db.{mastered,beaten,subset_compatible}` + `/api/status.subsets`.

## Future Work

- **Emulator integration**: Launch a game from the collection view
- **More ROM sources**: Vimm, ROMsFun, WowROMs, CDRomance are extensions in `extensions/` — install via `/extensions`
- **Notification on autodiscover**: Alert when Wanted pool grows via scheduler

## CI

`.github/workflows/docker-publish.yml` runs `pytest` as a **gate** (`build-and-push` `needs: test`) — broken code never builds or deploys. Run the suite locally with `venv\Scripts\python -m pytest tests/ -q`.
