# app/templates/ — Template Conventions & Gotchas

## Sidebar Navigation (`base.html`)

Nav order: Collection, Wanted, Search, Downloads, Settings, Scheduler, Logs.

Active-link detection:
- Exact match: `/collection`, `/wanted`, `/search`, `/logs`
- `startsWith`: `/settings`, `/scheduler`, `/downloads`

## Activity Tray Polling

- `base.html` polls `/activity/tray` every 3s via HTMX → updates sidebar tray
- `collection.html` polls `/activity/card-states` every 2s via JS → shows per-card overlays

## RA Badge Links

Wherever an RA match badge appears (`collection.html`, `library.html`, `download_item.html`), it is an `<a>` linking to `https://retroachievements.org/game/{ra_game_id}` when `ra_game_id` is known. Falls back to a plain `<span>` when ID is absent.

## Cover Refresh Button

Per-card "↻" hover button on collection and wanted cards.
- Only renders when `covers_enabled` is True (at least one source configured)
- Response replaces button with disabled "Fetching…" indicator
- Card-states overlay shows progress; page reload reveals new cover

## Scheduler "Run Now"

`POST /scheduler/run/{task_id}` holds the HTTP response until complete (synchronous, intentional). UI:
- Spinner via `hx-indicator` + button disabled while running
- "Last run" timestamp updates via `hx-swap-oob` — no page reload needed

## Docker Path Note

ROM file paths are stored as they appear **inside the container**. DB and covers persist at `/mnt/ssd_cache/appdata/rom-finder/` via Docker volume. Never store host-side paths in the DB.
