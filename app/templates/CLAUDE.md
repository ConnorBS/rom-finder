# app/templates/ — Template Conventions & Gotchas

## Sidebar Navigation (`base.html`)

Nav order: Collection, Dashboard, Wanted, Search, Downloads, Settings, Scheduler, Extensions, Logs.

Active-link detection:
- Exact match: `/collection`, `/wanted`, `/search`, `/logs`
- `startsWith`: `/dashboard`, `/settings`, `/scheduler`, `/downloads` (so `/dashboard/timeline` etc. highlight Dashboard)

## Charts (ApexCharts)

`base.html` loads **ApexCharts** via CDN (the only JS charting lib — graphs need it; everything else stays HTMX/minimal-JS). Only the `dashboard/*.html` pages use it. Pattern: server injects chart series as `{{ series | tojson }}` into a per-page inline `<script>` that `new ApexCharts(el, {...}).render()`. Dashboard pages use `theme: {mode:'dark'}` + `grid.borderColor:'#1f2937'` to match the UI. Filter pages re-render fully (plain GET form) so charts re-init cleanly — avoid HTMX-swapping a chart container. Tabs come from the shared `dashboard/_tabs.html` partial (set `active_tab` before `{% include %}`).

## Activity Tray Polling

- `base.html` polls `/activity/tray` every 3s via HTMX → updates sidebar tray
- `collection.html` polls `/activity/card-states` every 2s via JS → shows per-card overlays

## RA Badge Links

Wherever an RA match badge appears (`collection.html`, `library.html`, `download_item.html`), it is an `<a>` linking to `https://retroachievements.org/game/{ra_game_id}` when `ra_game_id` is known. Falls back to a plain `<span>` when ID is absent.

## Award badges, subset markers & multi-select (`collection.html`)

The cover top-left badge stack renders the RA award tier — **🏅 Mastered** (gold) / **✅ Completed** / **🏆 Beaten** — with **Mastered superseding** (first-match `{% if %}/{% elif %}`), plus subset markers (**⊂ Subset** copy, amber **⊕ N subset** available / muted **⊕ subset** when all compatible subsets are mastered). Driven by `item.mastered` / `item.ra_award` / `item.is_subset_rom` / `item.subsets_available`.

Multi-select is minimal JS (sanctioned, like the card-states poller): `.sel-check[data-lib-id]` checkboxes (card info area — kept off the cover so they don't trigger the detail slide-over; list view first column), a `#selection-bar` shown when the `window._sel` Set is non-empty, and `selectAllFiltered()` using `window.allFilteredLibIds`. The Set **resets on every full render** (filter/paginate/HX-Refresh), matching the filter→select-all→act flow. Action buttons post `library_ids` via `hx-vals` (body, not URL). `#col-perpage` (50–1000) is wired like `#col-system`; every nav link threads `&per_page`.

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
