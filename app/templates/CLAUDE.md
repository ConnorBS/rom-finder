# app/templates/ — Template Conventions & Gotchas

## Sidebar Navigation (`base.html`)

Nav order: Collection, Dashboard, Wanted, Goals, Search, Downloads, Settings, Scheduler, Extensions, Logs.

Active-link detection:
- Exact match: `/collection`, `/wanted`, `/goals`, `/search`, `/logs`
- `startsWith`: `/dashboard`, `/settings`, `/scheduler`, `/downloads` (so `/dashboard/timeline` etc. highlight Dashboard)

## Charts (ApexCharts)

`base.html` loads **ApexCharts** via CDN (the only JS charting lib — graphs need it; everything else stays HTMX/minimal-JS). Only the `dashboard/*.html` pages use it. Pattern: server injects chart series as `{{ series | tojson }}` into a per-page inline `<script>` that `new ApexCharts(el, {...}).render()`. Dashboard pages use `theme: {mode:'dark'}` + `grid.borderColor:'#1f2937'` to match the UI. Filter pages re-render fully (plain GET form) so charts re-init cleanly — avoid HTMX-swapping a chart container. Tabs come from the shared `dashboard/_tabs.html` partial (set `active_tab` before `{% include %}`).

## Activity Tray Polling

- `base.html` polls `/activity/tray` every 3s via HTMX → updates sidebar tray
- `collection.html` polls `/activity/card-states` every 2s via JS → shows per-card overlays

## Live in-place updates (idiomorph)

Pages update **in place** as background work changes the DB — no manual refresh, no
"refresh" prompt, and **without losing** scroll, an open detail panel, a multi-select,
or a half-typed input. Mechanism (all in `base.html` `<head>`, so the interval/listeners
register exactly once — a `hx-target="body"` filter nav re-runs body scripts and would
otherwise stack duplicate intervals):

1. A poller hits **`GET /api/changes`** every 4s — a cheap per-scope fingerprint feed
   (see `routers/CLAUDE.md`). It builds a fingerprint from the scopes the current page
   declares and compares to the previous poll (first poll = baseline; re-baselines when
   the page/scope changes).
2. On change it re-fetches **the current URL** (so query-string filters are preserved) and
   morphs the response's `#live-content` into the live `#live-content` via
   **idiomorph** (`hx-ext="morph"`, `swap:'morph:innerHTML'`, CDN `idiomorph-ext`). Morph
   diffs the DOM and only touches what changed — preserving focus, the active input's value,
   scroll, and untouched nodes. Then it dispatches a **`live:updated`** event.
3. The poller **defers** (skips the tick) while a text input/`select` inside `#live-content`
   is focused, so it never interrupts typing/selecting.

**Opt-in per page**: `<main id="live-content">` carries `data-live-scope` /
`data-live-mode` from two overridable Jinja blocks. Each page sets
`{% block live_scope %}…{% endblock %}` (space-separated scope names); empty ⇒ the page is
static (Settings/Extensions). Wired: collection=`library wanted`, downloads=`downloads hunts`,
wanted=`wanted library downloads`, goals=`goals`, logs=`logs`, scheduler=`scheduler`. **Dashboard** pages use
`{% block live_mode %}reload{% endblock %}` (ApexCharts can't survive a morph) so they do an
idle `location.reload()` when the mirror token changes — i.e. right after a manual RA Refresh
completes. Escape hatch: `localStorage.liveUpdates = 'off'`. Interval is hardcoded (4s), like
the 3s tray poll — no Settings toggle (no shared Jinja env to thread one in per route).

The detail slide-over, sidebar tray, and mobile indicator live **outside** `#live-content`,
so a morph never touches them. **Charts**: never morph a chart container — dashboard uses
reload mode for exactly this reason.

## RA Badge Links

Wherever an RA match badge appears (`collection.html`, `library.html`, `download_item.html`), it is an `<a>` linking to `https://retroachievements.org/game/{ra_game_id}` when `ra_game_id` is known. Falls back to a plain `<span>` when ID is absent.

## Award badges, subset markers & multi-select (`collection.html`)

The cover top-left badge stack renders the RA award tier — **🏅 Mastered** (gold) / **✅ Completed** / **🏆 Beaten** — with **Mastered superseding** (first-match `{% if %}/{% elif %}`), plus subset markers (**⊂ Subset** copy, amber **⊕ N subset** available / muted **⊕ subset** when all compatible subsets are mastered). Driven by `item.mastered` / `item.ra_award` / `item.is_subset_rom` / `item.subsets_available`.

Each card/list row also shows **base achievements earned/total** (`🏆 {item.achievements_earned}/{item.achievements}` = `RAGameProgress.num_awarded`/`max_possible` for the matched base game id, from the local mirror — subsets are separate ids so these are inherently the core set). Gold when `earned > 0`, muted when 0; **absent when there's no progress row** (never played / not in the mirror) — so "has a count" itself means "started." Surfaces in cards (under system·size) and a list **Achievements** column.

Multi-select is minimal JS (sanctioned, like the card-states poller): `.sel-check[data-lib-id]` checkboxes (card info area — kept off the cover so they don't trigger the detail slide-over; list view first column), a `#selection-bar` shown when the `window._sel` Set is non-empty, and `selectAllFiltered()` using `window.allFilteredLibIds`. **`window._sel` is initialised in `base.html`** (not the page's inline script) so it **survives a live in-place morph**; it still **resets on a real full render** (filter/paginate/HX-Refresh) via base.html's `htmx:afterSwap` handler that clears it when the swap target is `document.body`. The collection page re-applies it to checkboxes (`bindSelChecks()`) both at load and on the `live:updated` event after each morph. Action buttons post `library_ids` via `hx-vals` (body, not URL). `#col-perpage` (50–1000) is wired like `#col-system`; every nav link threads `&per_page`.

## Goals page filters/sort + delete-event

The Goals header has a toolbar (plain query-param controls, navigate via `goalsApplyFilters()`):
**Sort** (`?sort=event|due|added|title`), **Show completed** (`?show_completed=0/1`, default on),
**Show past-deadline** (`?show_past=0/1`, default OFF — overdue+incomplete goals are hidden, with a
"N past-deadline hidden" note). Filtering/sorting is server-side in `goals_page`; params survive the
live morph because the poller re-fetches the current URL. `due` orders cards by deadline (nulls last)
and event groups by their earliest deadline. **Un-completed goal cards render greyscale+dimmed**
(`goal_card.html`, `grayscale opacity-60`, color on hover); completed = full color. Each event header
has a **🗑 Delete event** button → `POST /goals/event/delete` (Form `name`) which deletes every goal
under it + its `GoalEvent` and returns `HX-Refresh`.

## Goals page — collapsible event groups

Each event `<section>` (in `#goals-groups`, keyed by `data-event-key`) has a ▾/▸ chevron
(`.event-collapse-btn`) that toggles its `.event-grid` (`hidden`) so you can navigate by headers;
a header **Collapse all / Expand all** pair calls `setAllGoalsCollapsed()`. Collapsed state persists
in `localStorage` (`goalsCollapsed:<event-name>`) and is re-applied on load AND on the **`live:updated`**
event (so an in-place morph doesn't blow it away — idiomorph drops the `hidden` class since the server
HTML lacks it, then `applyGoalsCollapsed()` restores it). Minimal JS, like the collection multi-select.

## Goals page (`goals.html`, `partials/goal_card.html`)

`/goals` lists event objectives grouped by `event_name` ("No event" last) with a "N of M done"
tally per group; opts into live morph via `{% block live_scope %}goals{% endblock %}`. The add
panel has two tabs (`switchGoalTab`): **RA game** (reuses the `/ra/search` system-select + query
inputs with `hx-vals='{"mode":"goal"}'`, results into `#goal-add-results`; a "Select →" button
calls `selectGoalGame(...)` to fill the add-form's hidden fields and enable submit) and **Custom
objective** (free-text game/system/objective). Both forms post into `#goal-feedback`; the new
goal then appears via the live morph. **`goal_card.html`** receives the per-goal `_card_ctx`
(`goal`, `progress`, `overdue`, `days_left`, `now`): objective badge (🏅 Master / 🏆 Beat / ✎
Custom), a live progress bar + `🏆 earned/total` + award badge from the joined `RAGameProgress`
row (absent when not started), and a three-state deadline line (green ✓ Completed / red ⚠ Overdue
/ amber-when-≤7d "📅 Due {date} (Nd)"). Actions: custom → "✓ Mark done"; completed → "↺ Reopen";
all → "✎ Edit" (toggles an inline event/deadline[/custom_text] form posting `/goals/{id}/edit`)
and a hover delete.

**Cover / locked state (per objective):** an **achievement** goal shows the achievement BADGE and
swaps the **locked vs unlocked** image by completion — RA appends `_lock` before `.png`, so an
active card renders `…/Badge/{n}_lock.png` and a done card `…/Badge/{n}.png` (NO greyscale).
A **game** goal (master/beaten/custom) keeps the cover with **greyscale+dimmed until done**.
Titles link to the **source**: the achievement title → `retroachievements.org/achievement/{achievement_id}`,
and the game name (the "from {game}" line on achievement cards, or the title on game cards) →
`retroachievements.org/game/{ra_game_id}` (plain text when the id is absent).

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
