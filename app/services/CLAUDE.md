# app/services/ — RA Integration, Sources, Scheduler

## RetroAchievements Integration

### API client (`ra_client.py`)
`RAClient(username, api_key)` — all methods async.

Key methods:
- `get_game_list(system_id)` → list of `{ID, Title, NumAchievements, DateModified}`
- `get_game_hashes_full(game_id)` → list of `{MD5, Name, Labels}`
- `lookup_hash(md5)` → game info dict if matched, else `None`. **Wrong-game guard:** a hash can match a *different* RA game than intended (a Solaris ROM downloaded during a Kirby hunt hashes to Solaris). BOTH verify paths must check the matched id equals the expected `ra_game_id` before marking verified — `downloads.py` (approval) and `hunter.py` (`_match_is_correct_game`, auto-hunt). A mismatch is recorded as `bad_hash` and the hunt continues.
- `get_game_info(game_id)` → full game detail (no achievement set)
- `get_game_extended(game_id)` → game detail **including the achievement set** (`API_GetGameExtended`); `get_achievements(game_id)` flattens it to `[{id,title,description,points,badge_url}]` (badge = `media.retroachievements.org/Badge/{BadgeName}.png`). Used to list a game's achievements for goal-setting + to enrich an achievement goal's card. Events are special "event games", so this also returns an event hub's achievement list given its game id.
- `search_events(query)` → events matching a name, via `get_game_list(RA_EVENTS_CONSOLE_ID=101)` (RA files event hubs under the "Events" console) + the same title-substring filter as `search_games`. Backs the Goals page's event name-search (`GET /ra/events/search`).
- `test_credentials()` → `(bool, message)`

`SYSTEMS` dict maps RA numeric console IDs → display names (authoritative list).  
`DEFAULT_FOLDER_MAP` maps system names → typical folder names on disk.

`RA_UNSUPPORTED_SYSTEMS` (+ `is_ra_unsupported(system)`): curated set of platforms RA has **no** console/hashing support for (e.g. `Nintendo 3DS`, `Archipelago`). ROMs on these can never hash-match, so the collection shows them as "unsupported" (not `no_ra`) and **every verify path skips them** so RA is never called for an unverifiable platform. Curated, NOT derived from `SYSTEMS` — misnamed-but-supported folders (`tg16`, `mega-duck-slash-cougar-boy`) DO verify, so excluding everything absent from `SYSTEMS` would wrongly hide real matches. Extend the set as such platforms appear.

**System name normalization (`title_utils.canonical_system(name, system_id)`)**: the RA console id is authoritative — resolve `SYSTEMS[id]` when present. Otherwise collapse an exact-doubled scraped name (`"WiiWii"` → `"Wii"`). It deliberately does NOT use an endswith/abbrev heuristic ("Super Nintendo Entertainment System" ends with NES's full name). Used by `api.py` (add-wanted + search) so corruption is fixed at the source regardless of what the Chrome extension posts; migration `0010` fixed existing rows, so the old every-startup WiiWii `UPDATE` is gone from `main.py` lifespan.

### RA V2 — wired (`ra_client_v2.py`, `events.py`)
**Live + Bearer-authed.** Two things V1 can't give, both pulled best-effort (failure never breaks import):
- **Event award tiers** — `get_event(id, include="awards")` → `tiers_from_event` → `[{title,kind,points_required,badge_url}]`, cached on `GoalEvent.tiers_json` (migration 0022). `events.fetch_event_meta` pulls tiers + `activeThrough` deadline in ONE call (used by `sync_event`). `_build_group` marks the current tier = highest whose `points_required ≤` the event's earned points; the event header renders the Bronze→Champion ladder.
- **Achievement source game · console** — `get_achievement(id, include="games.system")` → `source_game_from_achievement` → `{game_id,title,console}`. `events.fetch_source_game` + `events.enrich_source_games(event_game_id)` (background, one call per achievement, rate-limited) update each event achievement goal's `game_title`/`system` to the real source game, so the page subdivides by true game. Kicked after import + on nightly eventsync; the single-add path (`_enrich_achievement_goal`) also resolves it.

NB: per-achievement deadlines are confirmed **absent** from V2 (only `events.activeFrom/activeThrough` exist).

### RA V2 client (`ra_client_v2.py`) — historical note
`RAClientV2(api_key)` hits the JSON:API V2 service at **`https://api.retroachievements.org/v2`**
(separate host from V1's `retroachievements.org/API`), Bearer-auth, sharing the global 2 req/s
`_limiter`. `get_event(id)` / `get_achievement(id)` / `get_game(id)` back the **`/api/diag/ra-v2`**
probe (`?event=` / `?achievement=` / `?game=`, optional `&include=` override), which confirms
reachability/auth + captures the real payload (event `awards` tiers; achievement
`points`/`pointsWeighted` + source `games`; game `achievementSets`/`hashes`). Not yet wired into the UI — verify with the probe in a
deployment that has the key + RA network first (see root CLAUDE.md → V2 roadmap). Docs source:
`github.com/Chew/RA-api-docs/tree/feat/v2-docs/docs/v2`.
**V2 is JSON:API — it ONLY produces `application/vnd.api+json`**; `_headers()` MUST send that as
`Accept` (sending `application/json` returns HTTP **406**, confirmed via the prod probe 2026-06-11).
The same probe confirmed **V2 is reachable from the deployment** (clean JSON error envelope, NOT a
Cloudflare challenge — so it is NOT CF-gated). The probe now dumps a JSON:API error `{message, errors}`
body for any 4xx (`error_body`), so the next failure is self-diagnosing. **Auth scheme still unconfirmed:**
the client uses `Bearer`, but the official docs are inconsistent (V1 `?y=key` query param vs a base64
`username:apikey` header); the 406 is content-negotiation, returned before auth, so a clean re-probe
after the Accept fix is what tells us whether `Bearer` holds (200) or needs a different scheme (401/403).

### Cover filenames
`{ra_game_id}.png` when RA ID is known; `lib_{library_id}.png` for entries with no RA ID.

### RA hash algorithm (`rahasher.py`)
RA doesn't always use plain MD5 — many systems use platform-specific algorithms (NES strips iNES header, N64 uses specific byte range, etc.). `compute_ra_hash(path, system)` returns the correct hash or `None` if the system isn't handled. Caller falls back to `hash_rom()` (plain MD5).

**Canonical source for hash algorithms: rcheevos `rc_hash`.** Every RA-approved emulator (RetroArch cores, RALibretro, RAHasher) hashes via the rcheevos `rc_hash` library, so it is the source of truth — **derive each console's algorithm from it rather than reverse-engineering by trial.** When adding a new console: read the dispatcher in [`src/rhash/hash.c`](https://github.com/RetroAchievements/rcheevos/blob/develop/src/rhash/hash.c) (`case RC_CONSOLE_*` → `rc_hash_<console>`), then the implementation in `hash_rom.c` (cart/ROM) or `hash_disc.c` (disc). Port that exactly into `hasher.py` (`compute_ra_hash`/RAHasher already covers disc + many carts). For disc systems the RAHasher binary IS rc_hash, so prefer it.

**Arduboy is text, not binary** (`md5_arduboy` in `hasher.py`): `rc_hash_arduboy` → `rc_hash_text` emits each `.hex` line's content + a single `'\n'`, normalizing `\r\n`/`\r`/`\n` to `\n` and always appending a trailing newline. `md5_arduboy` is a faithful port of that loop. Hashing raw bytes made every CRLF dump (the "Erwin's Collection" repacks) miss while LF dumps matched — confirmed against RA's accepted hash for game 9286 ("Under the Tower"). `.hex` is in both `ROM_EXTENSIONS` sets so zipped/unzipped Arduboy files are scanned + extracted.

**RAHasher binary source**: RAHasher is **not** a standalone repo. It's distributed via `RALibretro` releases:
`https://github.com/RetroAchievements/RALibretro/releases/latest` → `RAHasher-x64-Linux-{version}.zip`
The Dockerfile downloads it at build time. Without it, disc-based systems (Saturn, PS1/2, Dreamcast, Sega CD, etc.) hash as plain MD5 of the image file and will never match RA's database.

**Wii/GameCube need nodtool**: RAHasher (RALibretro) **cannot read compressed GC/Wii images** (RVZ/WBFS/WIA/GCZ/CISO — RALibretro issue #415 unimplemented; in RetroArch the Dolphin core decompresses for rc_hash, but the standalone hasher has no decoder). `compute_ra_hash` detects a `_GC_WII_RA_IDS` system (16/19/20 — matched by resolved RA id, so folder names like "Nintendo Wii"/"Nintendo Gamecube" work) in a `_NODTOOL_FORMATS` container and **decompresses it to a temporary raw ISO via `nodtool`** (nod-rs, bundled in the Dockerfile) before hashing, then deletes the temp. The temp ISO lands in `check_dir/_convert/` (the review/staging area — **never** the ROM library), with a 15-min timeout.

**GameCube is hashed by a Python rc_hash port, NOT RAHasher**: the standalone RALibretro RAHasher's GameCube hash matches **0** of RA's DB (its GC method lags the server). So for GameCube (id 16) `compute_ra_hash` calls `hasher.md5_gamecube` on the converted ISO — a faithful port of rc_hash_gamecube/rc_hash_nintendo_disc_partition (partition header + main.dol code/data segments, big-endian, in order). Verified byte-exact vs RA (141/142 of the user's library; the 1 miss is a dump-revision difference). Wii/Wii U still use RAHasher (which works for them). RAHasher failures log to `applog` category `hash` (HTTP-visible).

**Disc images inside a `.zip`/`.7z` are unwrapped first**: RAHasher (and `md5_gamecube`) can't mount a disc from within an archive — handed the archive directly it fails, then the plain-MD5 fallback can never match RA's disc hash (the Little Britain `.7z` / Dracula EU `.zip` cases). So for a **disc system** (`_is_disc_system` → `DISC_SYSTEMS` or a GC/Wii id) `compute_ra_hash` extracts the archive to `check_dir/_convert/<stem>.extract/` and points the hasher at the disc descriptor (`.cue`/`.gdi` preferred so its data tracks resolve alongside; else the largest image), then `rmtree`s the dir. Composes with the nodtool step (a `.rvz` inside a `.zip` extracts → converts → hashes). Cartridge `.zip`s are left alone (RAHasher reads them natively). This is what lets an archived copy match its extracted `.cue` sibling's hash → `recompute_duplicates` then tags it. The unwrap is scoped to disc systems to avoid disturbing the multi-ROM `prefer_name` logic in `hasher._hash_from_archive`.

### Save-file detection (`saves.py`) — READ-ONLY
`scan_saves(session)` flags which games have an emulator save, matching saves to ROMs by **filename stem** (RetroArch & most emulators name a save after the loaded content: `Game (USA).nes` → `Game (USA).srm`/`.state`/`.state1`…). **Wii / Dolphin saves** are *also* detected, but keyed by **title-ID-low** (4 ASCII chars in the disc header, e.g. `RKME`), because Dolphin writes per-game saves under `User/Wii/title/{type}/<hex(title_id_low)>/data/` (not by ROM name). `_read_wii_title_id` extracts the ID from `.iso` (offset 0), `.rvz`/`.wia` (offset 0x58, the embedded disc-header copy), and `.wad` (parses cert chain + ticket → title ID low at ticket+0x1E0) — fast-path only, no nodtool decompression. Populated lazily into `LibraryEntry.disc_id` (migration 0016). PS2/GameCube/PS1 are inherently un-detectable here — those emulators use shared memory-card files (`Mcd001.ps2`, `Card A.raw`), not per-game saves. **`wii-nand` kind** renders as an emerald badge in the detail panel. It scans `saves_dir` + the ROM roots (`download_dir`/`check_dir` — saves often sit beside the ROM) for battery (`.srm`/`.sav`/`.mcr`/`.gci`/…) and save-state (`.state`/`.stateN`/`.stN`/`.ssN`) files, normalizes each save's stem (also stripping a kept ROM ext, `Game.nes.srm` → `Game`), and writes `save_count`/`save_files`/`save_updated_at` per entry (full rebuild). **CRITICAL: it only ever reads (`rglob`+`stat`) — never opens-for-write, moves, renames, or deletes a save, and the library delete-file action only removes ROM/disc-track extensions, never save extensions.** Run after scan/folder-scan + a manual "Scan saves" button (`POST /collection/scan-saves`). Surfaces as a 💾 badge, a `has_save` filter, a header count, and the detail panel's read-only Save-files list.

### Duplicate detection (`duplicates.py`)
`recompute_duplicates(session)` tags redundant `LibraryEntry` copies via `duplicate_of` (canonical sibling's id; `None` = canonical/unique). It's a **full rebuild** (clear then re-derive) so resolved/removed dups clear — never append-only. Entries are unioned (union-find) when they share **identical hash** or the same **normalized title+system**; each group's canonical = RA-matched, then non-archive, then lowest id. **`ra_game_id` is deliberately NOT a grouping key** — RA files many genuinely different ROMs under one game id (hack collections, "subset" games; e.g. dozens of distinct SM64 romhacks share one id), so grouping by it mass-mistagged unrelated games (1302 → 139 once removed). **Subset rules (user requirement):** different discs of a multi-disc game never merge (the disc marker lives in the title, so "(Disc 1)" ≠ "(Disc 2)"); `.bin`/`.img` tracks are excluded entirely (a track is a component of its `.cue` — tagging/deleting it would break the disc); and **RA "Subset" entries** (filename/title contains "(Subset" / "[Subset", e.g. `(Subset - Glitch Showcase)`) are excluded too — they are an intentional second copy of the same ROM kept to track a separate achievement set, so both copies must stay (neither is tagged, neither causes its sibling to be tagged). LOCAL only (no RA calls); cheap enough to run after every scan/rehash/verify pass.

**Availability is surfaced (Phase 6), not silent:** `rahasher_status()` powers `/api/status.rahasher`; `main.py` startup also writes an `applog` info/warning (HTTP-visible via `/logs`, not just Docker stdout); and `/settings` shows an amber banner when it's missing. **Disc guard:** all hashing goes through `ra_hash_or_fallback(path, system) -> (hash, used_rahasher)`, which logs a clear WARNING when a `DISC_SYSTEMS` ROM falls back to MD5 because RAHasher is absent (`disc_without_rahasher()`), so it's diagnosable instead of masquerading as "not in RA database".

### Rate limiting

`_RateLimiter` (module-level `_limiter`) enforces 2 req/sec (120/min) across all `RAClient` instances and methods. RA's documented ceiling is 500 req/min; 120 gives a comfortable buffer — 4 req/sec still triggered 429s in practice. On a 429 response, `lookup_hash` waits for the `Retry-After` header value (default 60s) and retries once. A second consecutive 429 raises so the caller skips the entry and continues.

At 2 req/sec, a full bulk verify of ~10 000 entries takes ~83 minutes as a background task.

On a persistent 429, `lookup_hash` now raises **`SourceRateLimitError`** (from `sources/errors.py`, carries `retry_after`) instead of `RuntimeError`, so the resilient verify can back off intelligently.

### Resilient re-verify (`ra_verify.py`)
`run_pass()` is the rate-limit-aware, resumable bulk verify (replaces the old in-memory loop that the 2026-05-03 429 cut off, stranding ~2789 entries). It:
- derives the work set from the DB each pass (`repository.library_pending_ra_check` — hashed, unmatched, and `ra_checked_at` null or older than `stale_days`), so a crash/restart just recomputes the remainder;
- opens a **fresh Session per entry** (never across the `lookup_hash` await);
- on `SourceRateLimitError`, persists an escalating pause in `ra_verify_paused_until` and stops — the next pass (scheduler tick or manual run) honours it even across restarts;
- stamps `ra_checked_at` so genuine misses leave the work set (passes terminate; the daily scheduler pass doesn't re-hammer them).

Wired as the scheduler **`verify`** task (`sched_verify_*`, default 05:00) and the `/scheduler` Run-now button. State surfaces in `/api/status.verify`. Settings: `ra_verify_in_progress`, `ra_verify_paused_until`, `ra_verify_last_run`, `ra_verify_batch_size`.

### RA Dashboard mirror sync (`ra_dashboard.py`)
`refresh()` builds the **local mirror** the dashboard reads from (so browsing makes zero RA calls). It fetches profile + every unlock (`get_achievements_earned_between`, member-since→now in `ra_dashboard_window_days` windows, de-duped on `(achievement_id, hardcore)`) + paginated completion (`get_user_completion_progress`) + awards, then **replaces `ra_achievement`/`ra_game_progress` in one transaction** — wholesale replace is how retroactive RA changes reconcile. **Per-game backfill (step 2b):** `get_achievements_earned_between` **under-reports individual unlocks** — it returned only 14 of the user's 28 earned AotW unlocks, and it also dropped a Jun-24 hardcore unlock on game 14811 that had an achievement goal, so the goal never auto-completed. So after the windowed pass, refresh pulls **per-game progress** via `get_game_user_progress` (`API_GetGameInfoAndUserProgress`) for **each tracked event hub** (distinct `GoalEvent.ra_game_id`) **AND the source game of every achievement goal** (distinct `Goal.ra_game_id` where `objective == achievement` — a *custom* event has no hub id, so its games would otherwise be skipped), and `setdefault`-merges any earned unlock the windows missed (the windowed entry wins where present). This is what makes an achievement goal on a regular game auto-complete after a Refresh. Diagnose the gap with `GET /api/diag/user-game-progress?game_id=<hub>` (`earned_but_missing_from_mirror`). Fresh `Session` per await-gap; all RA calls use the shared 2 req/s `_limiter`; progress reported via `activity_store` (`ra-sync`); stamps `ra_dashboard_last_sync`. **Manual only** (no scheduler task). **429 resilience (why "Refresh did nothing"):** the windowed pull makes ~50 sequential `get_achievements_earned_between` calls, and that method previously had a bare `raise_for_status()` — a single transient 429 in ANY window raised straight out of `refresh()`, aborting the whole sync so the mirror stayed stale with no signal (the user's "I refresh but get no new unlocks"). Now `get_achievements_earned_between` + `get_user_completion_progress` go through `RAClient._get_with_retry` (retry ONCE after `Retry-After`, then raise `SourceRateLimitError`), matching `lookup_hash`. And `refresh()` **stamps the outcome** — `ra_dashboard_last_status` (`ok`/`rate_limited`/`error`) + `ra_dashboard_last_error` (with a timestamp) — on BOTH success and failure. A failed run never reaches the replace step, so the prior mirror is preserved (stale but intact), and the status is surfaced as an amber banner on the overview + folded into the `dashboard` `/api/changes` fingerprint so the page **auto-reloads to show the banner even though `last_sync` didn't advance**. New `RAClient` user methods it relies on: `get_user_profile`, `get_achievements_earned_between` (Unix `f`/`t`), `get_user_completion_progress` (paginated), `get_user_awards`. At the end it also runs `mastery.sync_library_awards` + `subsets.recompute_subset_flags` + `goals.evaluate_goals` (all LOCAL) so the collection's badges + goal completions reflect the fresh mirror immediately. **Date-format gotcha (`_parse_dt`):** RA endpoints are inconsistent — `API_GetAchievementsEarnedBetween`'s `Date` is space-separated `"%Y-%m-%d %H:%M:%S"`, but `API_GetUserCompletionProgress`'s `HighestAwardDate`/`MostRecentAwardedDate` are **ISO-8601 with a `T` + timezone offset** (`"2025-02-02T00:00:00+00:00"`). `_parse_dt` MUST handle both (it falls through to `datetime.fromisoformat`, normalizing to naive-UTC); otherwise every award date silently parses to `None` → master/beaten **goals auto-complete with the sync date instead of the real mastery date** (the dashboard Games "Last" column shows "—" for everything is the tell), and the dashboard "recent" sort breaks. Fixed 2026-06-25; the test fixture must use the real ISO form, not the space form, or it masks the regression.

### Award tiers (`mastery.py`) — LOCAL
`sync_library_awards(session)` stamps `LibraryEntry.ra_award` (mastered|completed|beaten|beaten-softcore|"") from the dashboard mirror — zero RA calls, full rebuild. **"mastered" = hardcore 100%** (`highest_award_kind == "mastered"`); subsets do NOT gate it. A subset copy usually shares the base game's hash (so its `ra_game_id` points at the base), so subset entries (`duplicates._is_subset`) resolve their award by **normalized title first** (`base_title` strips the `[Subset …]`/`(Subset …)` suffix), falling back to `ra_game_id`.

### Goal auto-completion (`goals.py`) — LOCAL
`evaluate_goals(session)` flips every active, non-custom `Goal` to completed when the local RA
mirror satisfies it — zero RA calls. **Hardcore-only** across the board: `master` ⇐
`highest_award_kind == "mastered"`, `beaten` ⇐ `in ("beaten","beaten-hardcore","mastered")` (both
from `ra_game_progress` via `award_satisfies`), and `achievement` ⇐ a **hardcore** `ra_achievement`
row exists for the goal's `achievement_id`. Softcore awards/unlocks never count; `custom` never
auto-completes. **⚠ RA's API returns `"beaten-hardcore"` (and `"beaten-softcore"`) for the beaten
tiers — NOT plain `"beaten"`.** `award_satisfies` (and `mastery._BEATEN_KINDS` / `collection._BEATEN_KINDS`
/ the collection Beaten badge) must therefore accept `"beaten-hardcore"`; the original code only
checked `"beaten"`, so every hardcore-beaten (non-mastered) game silently never completed its Beat
goal nor got a Beaten badge (fixed 2026-06-13). `beaten-softcore`/`completed` stay excluded (hardcore-only).
**Event clones are matched by their OWN id (trust the import id).** RA's **AotW / RA Roulette**
event tiles are *clones* with their own `achievement_id` under the event hub — distinct from the
source-game achievement. But RA records the clone as **its own hardcore unlock** (the user's
timeline shows two rows for one earned achievement: the source-game one AND the event-clone one,
each a different id). The import stores that same clone id, so a plain `achievement_id` match is
correct and sufficient — do **not** add badge/title fuzzy-matching (tried + reverted; "trust the
achievement id it comes with"). If an imported event goal isn't completing despite the user having
earned it, the cause is the **mirror missing that clone unlock**, not the match logic — diagnose with
**`GET /api/diag/user-game-progress?game_id=<hub id>`** (RA's per-achievement truth →
`ra_earned_hardcore`, `earned_but_missing_from_mirror` >0 = mirror PULL GAP) and
**`GET /api/diag/goal-mirror?event_game_id=<hub id>`** (goal ids vs mirror).
Stamps `auto=True` + `completed_at` = **the REAL RA date** (the achievement's hardcore `earned_at`,
or the game's `highest_award_date`/`most_recent_date` for master/beaten) — NOT the evaluator's run
time (`updated_at` stays now so the live fingerprint still advances). A second pass **self-heals** the
`completed_at` of already-auto-completed goals whose date predates this (re-stamps from the mirror when
it differs; idempotent). Returns `{completed, corrected}`.
**Source game/console resolution for event goals (`resolve_event_source_games`, LOCAL):** an event
clone's V2 `games` link points at the event hub, not the real game, so to show "from {real game} ·
{console}" we **match the achievement's NAME + DESCRIPTION against the mirror's NON-event achievements**
(real game + console). Exactly one distinct source game → set `game_title`/`system`; zero or AMBIGUOUS
(>1) → leave unresolved. Only touches event-hub goals (`system == "Events"`), so resolved ones are
skipped. Description match strips a leading event game tag (`_DESC_TAG`, e.g. AotW's `"[FF1] "`) before
comparing. `ra_game_id` is deliberately NOT changed (kept = hub id for import dedup). Called after
`evaluate_goals` on `/goals` load + dashboard refresh. (Supersedes the broken V2 `events.enrich_source_games`,
which used `include=games.system` — a 400 — and resolved only to the hub anyway.)
Called from `ra_dashboard.refresh()` (after `sync_library_awards`/`recompute_subset_flags`) and
on every `/goals` page load — the only two moments the mirror can change. Deliberately NOT a
scheduler task (the mirror is manual-refresh only).

### Event import + nightly sync (`events.py`)
`sync_event(game_id, …)` imports every achievement of an RA event/game hub as `achievement`
goals in **ONE** `API_GetGameExtended` call (never holds a DB session across the await). It skips
**placeholder tiles** (BadgeName `00000` → empty badge_url), de-dups against existing goals, and —
when `include_completed` is False — skips achievements already earned in hardcore. It records/refreshes
a `GoalEvent` (`auto_sync=True`) and runs `evaluate_goals` so imported-as-already-earned ones flip to
completed. `build_event_goals` is the pure-DB core (reused by import + sync); `parse_event_ref` pulls
the id from a pasted URL/`/game/N`/`/event/N`/bare number. `sync_all_auto()` is the **nightly** pass
(scheduler `eventsync` task): one call per auto-sync event (globally rate-limited to 2 req/s), adding
any newly-published achievements — that's how AotW/random-roll events grow over time. Stores the
achievement's own RA `points` per goal (NB: this is the achievement's points, **not** the event's
own point value, which the V1 API doesn't expose). `routers/goals._refresh_goal_art` re-pulls badges
(one call per distinct game) + box art on demand, also rate-limited. **Event deadline auto-pull**:
when an import gives no explicit deadline, `sync_event` best-effort fetches the event's end date from
RA **V2** (`events.fetch_event_deadline` → `/events/{id}` `activeThrough`) and stamps it on the goals +
the `GoalEvent` (so nightly-added achievements inherit it). Best-effort — V2 unreachable
(Cloudflare/bad key) just means no auto-deadline; gated by setting `event_pull_deadline` (default true).

**Achievement-goal enrichment** (`routers/goals._enrich_achievement_goal`, background): on add, an
achievement goal is enriched from `RAClient.get_achievements(game_id)` — canonical title
(`custom_text`), `achievement_desc`, and the **badge image** stored as an absolute `cover_path`
(`media.retroachievements.org/Badge/…`; the card uses it directly — an HTTP page may load an HTTPS
image, so no download). Same task backs both `POST /api/goal` (extension) and `POST /goals/add` with
an `achievement_id` (the page's achievement-count picker).

### Hub import (`hubs.py`)
`fetch_hub_games(hub_id)` paginates V2 `/hubs/{id}/games?include=system` (no DB session across the
await) → `[{game_id, title, console, achievements, points, players}]` (`RAClientV2.games_from_payload`,
extended to carry `achievementsPublished`/`pointsTotal`/`playersTotal`). `progress_bucket(award,
num_awarded)` is a **pure, LOCAL** helper that classifies a game by the configured user's RA-mirror
progress → `none`/`some`/`beaten`/`mastered` (mirrors `mastery._BEATEN_KINDS`; hardcore). `POST
/wanted/import-hub` is now a **preview** (`routers/wanted`): it annotates each game with owned/wanted +
the bucket and renders `partials/hub_preview.html` (filterable + per-game deselect). `POST
/wanted/import-hub/add` adds only the kept games (base64(JSON) tokens, no second RA fetch). **V2 has no
game-type (retail/hack) or set-type field** — those RA hub filters can't be reproduced from this API.

### Hash-aware subsets (`subsets.py`)
Two layers. `refresh_subset_cache(game_ids=None)` is the **only RA-calling** part: for each owned game it enumerates subsets from the per-console `get_game_list` (same call autodiscover makes — so newly-published subsets are picked up on re-run) and pulls each subset's `get_game_hashes_full`, replacing `ra_subset_hash` wholesale (`game_ids=None`) or per-parent (scoped). `recompute_subset_flags(session)` is **LOCAL**: joins each ROM's hash against the cache + the mirror's subset mastery and writes `is_subset_rom` + `subset_info`. **Matching is by hash only** — a subset needing a patched ROM the user lacks never matches. Subset discovery rides the recurring RA passes: **autodiscover** runs the full owned sweep (catches new subsets on owned titles); **bulk verify** / `ra_verify.run_pass` / single `/library/{id}/verify-ra` run scoped refreshes for the games they matched; plus the manual **Detect subsets** button (`POST /collection/sync-subsets`). `sync-awards` (`POST /collection/sync-awards`) re-derives awards + flags locally.

### RA API gotchas

**`API_GetGameInfoByMD5` is deprecated/broken**: Returns HTTP 404 for ALL hashes including ones confirmed in RA's database via `API_GetGameHashes.php`. `lookup_hash` now uses `dorequest.php?r=gameid&u={user}&m={hash}` instead, which returns `{"Success": true, "GameID": N}` (N=0 = not found, N>0 = found). This was confirmed by cross-checking: `API_GetGameHashes.php` returned hash `3132056c8f17e4088b95e4264ca59575` for game 724, but `API_GetGameInfoByMD5.php` returned 404 for that same hash.

**`dorequest.php` response format**: `{"Success": true, "GameID": N}` — normalised to `{"ID": N, ...}` before returning to callers.

**Legacy `API_GetGameInfoByMD5` response formats** (kept for reference):
1. Flat with `"ID"` — legacy format: `{"ID": 1234, "Title": "...", ...}`
2. Flat with `"GameID"` — emulator-style: `{"Success": true, "GameID": 1234}`
3. Wrapped in `"GameData"` — newer API format: `{"GameData": {"ID": 1234, ...}}`

All are normalised to `payload["ID"]` before returning. Callers always use `match.get("ID")`.

**Null response**: When hash not found, API returns JSON `null`. `lookup_hash` guards with `isinstance(data, dict)`.

**HTTP 404**: Returned for "not found" — handled by returning `None`, not raising.

**HTTP 429**: Raises `RuntimeError` with rate-limit message so caller's warning log is actionable.

**Non-JSON response** (e.g., Cloudflare challenge page): `lookup_hash` catches the parse error, logs a warning via Python `logging` (visible in Docker logs), and returns `None`.

**RA hash not matching**: Usually means the dump isn't in RA's hash list (different No-Intro dump). Check `/logs` → category "hash" — exact hash is logged. If `lookup_hash` returns no game ID despite a valid-looking response, a `WARNING` is emitted to Docker stdout with the raw response body.

---

### CHD format check (`chd_format.py`)
The in-app twin of the `R:\Roms\<console>\_Convert-to-CHD.bat` scripts. A CHD compressed with
**Zstandard** (`cdzs` for CD images, `zstd` for DVD/raw) is a valid, hash-correct dump that plays
fine, but RetroArch's *RetroAchievements* hasher (its bundled libchdr) can't decode the zstd hunks,
so the game boots and earns **no achievements**. Two halves:
- **Detection — pure, no binary.** `read_chd_codecs(path)` reads the CHD **v5 header** (32 bytes:
  tag `MComprHD` + version at offset 12 + four compressor FourCCs at offset 16) and returns the codec
  list (pre-v5 → `[]`, can't be zstd; not-a-CHD → `None`). `chd_status(path)` → `"cdzs"`/`"zstd"`
  (needs re-encode), `"ok"`, or `""`. Validated against real chdman-0.283 files.
- **Conversion — best-effort, needs `chdman`.** `convert_chd(path)` runs `chdman copy` to a temp file
  with `cdlz,cdzl,cdfl` (CD) / `lzma,zlib,huff,flac` (DVD), confirms the new **Data SHA1 == original**
  (disc data identical → RA hash unchanged), then `os.replace`-swaps. Exec'd with an arg list (no
  shell) so spaces/`( )`/`!` in names are safe. **NB:** a `chdman` too old to *read* zstd (the distro
  package) re-encodes non-zstd fine but fails on a cdzs input — surfaced, so it falls back to flag-only.

`run_chd_check(convert=None)` walks owned `.chd` entries, stamps `LibraryEntry.chd_codec` (migration
0024), and re-encodes the zstd ones when enabled + chdman present (fresh session per file write, never
across the chdman await; per-card overlay via `chd-check-batch`). **Gated on the `chd_format_check_enabled`
opt-in** (returns `{status:"disabled"}` otherwise). Wired as the scheduler **`chdcheck`** task
(`sched_chdcheck_*`, double-gated on the master flag, default off) + its Run-now button. `chd_format_status(session)`
is the DB-only snapshot for `/api/status.chd` + the Settings panel (counts off the stamped column +
chdman availability, no file I/O).

## Library roots — multiple ROM directories (`library_roots.py`)

The library is the **union of all registered `LibraryRoot` directories**, not one `download_dir`.
This leaf-ish module is the single home for root logic (so routers/scheduler don't duplicate it):
- `get_roots` / `primary_root` — ordered roots / the one `is_primary`.
- `iter_rom_files(roots, cue_cache)` — the **one shared on-disk walk** reused by all three scan paths
  (`collection.bulk_scan`, `library /scan`, `scheduler.run_scan`); yields `(root, system, title, fname, fpath)`
  and lazily imports `ROM_EXTENSIONS`/`is_disc_track`/`_rom_title` from `routers.library` (same pattern
  scheduler already used) to avoid a module-load services→routers import.
- `resolve_system(root, folder)` — per-root **folder→system** map (then built-in default aliases, then the
  folder name). `dest_folder_for_system(root, system)` inverts it (the folder to FILE a download into).
- `download_target(session, system)` → `(base_dir, folder, root_id)` — where downloads/hunts/moves file
  (the **primary** root). Every download chokepoint (`downloads._do_approve_move`/`_run_download`,
  `hunter.auto_hunt`, `external_hunt._staging_dir` + ingest) routes through it and stamps `root_id`.
- `root_for_path(roots, path)` — longest-prefix match (assigns `root_id` on backfill + resolves a move's source).
- **`reconcile_primary_path(session)`** — keeps the primary root's `path` == the `download_dir` setting.
  Called at the scan/download chokepoints + by `ensure_primary_and_backfill`, so anything that sets
  `download_dir` directly (the Settings ROMs-dir field, tests, env defaults) still drives the primary dir.
- `ensure_primary_and_backfill(session)` — lifespan startup: seeds the primary from `download_dir`
  (migrating the **reversed** legacy global `folder_map` into its folder→system map), guarantees one
  primary, then backfills any null `LibraryEntry.root_id`. Idempotent.

**Moving** (`routers/collection._relocate` / `_bulk_move`): relocates a ROM (+ its `.cue`/`.gdi` tracks)
into another root's console subfolder, updating `file_path`+`root_id`. Fresh sessions only — the blocking
`shutil.move` runs in an executor with NO session held (so the pool isn't drained on a big cross-fs copy,
mirroring `_do_approve_move`). Refused on a read-only source/dest or a name conflict at the destination.

## ROM Sources (`sources/`)

Each source extends `BaseSource`:
- `source_id: str`
- `name: str`
- `available: bool` — False = UI shows "coming soon", endpoint disabled
- `async search(query, system) → list[dict]`

Implemented: `archive_org`, `cdromance`, `vimm`, `wowroms`, `romsfun` (last four are extensions).

**Vimm uses Chromium for ALL three operations (search, get_files, download), not just download.** Vimm now bot-blocks plain HTTP clients site-wide — httpx to the homepage, a vault game page, AND the search listing all hang (`ReadTimeout`). The old code used httpx for search/get_files (only download used Playwright), so both silently returned nothing (`try/except → return []`) and **Vimm contributed zero candidates to every hunt** even when it had the game (the "Vimm reported entries but no download came back" report). `_browser_get_html(url)` fetches the rendered page via headless Chromium; `search`/`get_files` call it, serialized under `_get_vimm_lock()` so only one Vimm browser runs at a time. Search dropped the old httpx-era word-dropping recursion (each retry is now a full browser launch; the hunt already retries with multiple queries) and keeps a single no-system fallback.

### Auto-hunt candidate matching (`hunter.py`)
Each source returns candidate files; the hunter scores them against **RA's accepted ROM names** (`_file_score`) — that's the authoritative "is this the right dump?" check. Key rules:
- A score of **0 = unrelated** (matches neither an RA ROM-name stem nor the game title's significant words). Always skipped — works even when RA hashes failed to load (no region freebie). Stops a loose collection match (e.g. an NDS romset for a Wii hunt) from being downloaded.
- `title_utils.significant_terms(title)` provides a title fallback when RA stems are unavailable (`_significant_terms` is an alias kept for the existing import/test).
- **Hard cap** `_MAX_CANDIDATES` (40) on files attempted per hunt — prevents the "hundreds of downloads" flood while leaving room to exhaust a multi-source/region candidate list.
- Token-CDN sources (ROMsFun, WowROMs) can 403 from anti-leech protection even with a fresh signed token; these surface as `SourceForbiddenError` and the hunt moves on. **Archive.org (direct download, no token) is the reliable backbone.**
- **A download failure is retryable, a bad hash is not.** The hunt records `download_failed` (CDN 403 / timeout / network) vs `bad_hash`. Because a token CDN 403s *intermittently even on a good file*, the block set (`past`/`past_urls`, rebuilt from `HuntAttempt` rows each pass) only blocks a `download_failed` file after `_MAX_DOWNLOAD_RETRIES` (3) failures — a `bad_hash` blocks at once (re-downloading the same dump yields the same hash). So a file that merely 403'd is re-attempted on later hunts and recovers once the CDN lets it through (or the download path is fixed). Hunt History shows `↻ retried on later hunts` for failures vs `⊘ blocked for this game` for bad hashes.
- **Each download attempt shows a live progress card + Cancel** — the hunt creates a transient `Download` row (`status=downloading`, `file_path=None` to dodge the `ux_download_path` unique index, `hunt_task_id="hunt-{wanted_id}"`) and passes a real `progress_callback` so the Downloads page/tray show byte-level progress (previously hunt downloads passed `None` and created no row until success → no visible progress). On **verify it REUSES that row** (promoted to `pending_approval`, `hunt_task_id` cleared) — never a 2nd Download; on **bad_hash/failure it deletes the row** (the `HuntAttempt` is the durable record); a `finally` safety-net drops any orphaned transient row. The card's Cancel posts to `/activity/cancel/hunt-{wanted_id}`. `_MAX_CANDIDATES` raised 20→40 (still a flood-guard).
- **Cancel is responsive AND terminal.** The download runs through `_download_with_cancel(coro_factory, task_id)`, which wraps `download_file` in a task and polls the cancel flag every `_CANCEL_POLL` (2s), so Cancel aborts an in-flight download promptly instead of waiting out the 5-min per-attempt timeout (it raises `_HuntCancelled`; the attempt deletes its transient card and records NO `HuntAttempt` — a cancel isn't a real "try"). A cancelled hunt then **returns before `_try_external`** (a post-loop `is_cancelled` check) so it is never handed to the torrent/usenet client — the bug where Cancel parked the game in `awaiting_external`, polled forever, no card to cancel. The `finally` safety-net **skips any `Download` referenced by a non-terminal `ExternalDownload`**, so the legitimate long-lived external progress card (same `hunt_task_id`, created by `submit_external` on normal exhaustion) isn't deleted out from under the scheduler poll. `finish(task_id)` clears the cancel flag so a later re-hunt isn't auto-cancelled by a stale flag.
- **Orphan reaper survives a hard restart** (`reap_orphaned_hunt_downloads(task_id=None)`). The `finally` net only fires if the coroutine *reaches* it — a **SIGKILL on redeploy** (Watchtower) kills it without `finally`, so the transient `downloading` card is stranded AND un-cancellable (the in-memory hunt task is also gone, so the Cancel button's `activity_store.cancel` is a no-op). This shared helper deletes such rows — `hunt_task_id` set, status in `downloading/hashing/verifying`, and **NOT** backed by a non-terminal `ExternalDownload` — scoped to one `task_id` (the `finally` net + the Cancel-endpoint fallback) or all of them (`main.py` lifespan startup, since a restart kills every hunt coroutine). It is the single implementation behind all three call sites. This is what fixes the reported "a game keeps trying to download and I can't cancel it" after a deploy.
- **Skips the hunt when an owned ROM already satisfies the game** (`_owned_accepted_copy`). Before searching, `auto_hunt` checks whether any `LibraryEntry.file_hash` is in `ra_hashes` (the wanted game's accepted-hash list); if so it marks the WantedGame `verified` and returns — no download. This is the **subset** case: a Subset's `ra_game_id` differs from the base game's and the subset reuses the base ROM (SM64 "Coin Collector" accepts plain `Super Mario 64 (USA)`), which the user owns under the BASE id, so add-wanted's `ra_game_id` ownership check can't see it and the hunt would otherwise download a byte-identical duplicate.
- **Verification accepts the wanted game's OWN accepted-hash list, not just `lookup_hash`** (`_verified_game_id`). A dump verifies when RA's hash lookup returns the expected id OR the dump's hash is in `ra_hashes` (= `get_game_hashes_full(ra_game_id)`). The second path is how a **Subset** game verifies: its `ra_game_id` differs from the base game's, so `lookup_hash` resolves the base ROM's hash to the BASE id — but the subset reuses that ROM and its hash IS in the subset's list, so it verifies as the wanted (subset) id. A hash matching a *different* game NOT in the list (a Solaris ROM during a Kirby hunt) is still rejected.

**Match terms come from `search_title(game_title)`, not the raw or stored title.** A raw RA title can embed a platform suffix (`"Ristar (Genesis/Mega Drive)"`) or a `"[Subset - …]"` tag; those words otherwise become *required* match terms, so the real ROM (`Ristar`, `Super Mario 64 (USA)`) is judged irrelevant AND a whole-system romset (whose name contains "genesis/mega/drive") outscores it. **Two distinct title functions** (`title_utils`):
- **`clean_title`** — strips RA suffixes + platform parens (incl. slash combos like `(Genesis/Mega Drive)`) but **KEEPS the `[Subset …]` tag**. This is the **stored/displayed** title (`api_add_wanted` stores `clean_title`), so a subset keeps its identity and `duplicates`/`mastery` (which key off the `[Subset]` marker) still work. (Regression caught 2026-06-01: when `[Subset]` stripping briefly lived in `clean_title`, extension-added subsets lost their tag in the DB — moved to `search_title`.)
- **`search_title`** — `clean_title` **plus** the `[Subset …]` tag stripped. Used for SOURCE SEARCH + relevance (`hunter.title_terms`, `wanted.want_terms`, `search_variations`), because a hash-only subset reuses the base game's ROM, so we search/match the base. This was the "Vimm/sources reported entries but the game stayed exhausted" cause for platform-disambiguated and subset titles.

**"search == hunt" — shared result relevance (`title_utils.title_is_relevant`).** A source's loose full-text search surfaces *sibling* titles (searching "Pajama Sam: Don't Fear the Dark" returned a *different* "Pajama Sam" game). `title_is_relevant(candidate_title, want_terms)` encodes the **same** accept rule as `_file_score`'s title fallback (every significant word present, or — for ≥3-word titles — all-but-one), so:
- The Wanted-page source search (`wanted.wanted_source_results`) and the RA-game source lookup (`games.ra_game_sources`) **drop results that don't name the wanted game** — the panel shows only what the hunt would accept.
- All three query loops (those two + `hunter.auto_hunt`) now **stop at the first query that yields a *relevant* result**, not merely *any* result — a junk-only early query (e.g. an RA ROM-name stem the site can't match) no longer short-circuits a better later query.

---

## Torrent / Usenet download client (`download_clients/`, `external_hunt.py`) — LAST RESORT

A **third extension type** (`type="download_client"`, beside `rom_source`/`cover_source`) for async/external downloads. The shipped one is `extensions/download_client.py` (`TorrentUsenetClient`): Prowlarr search → qBittorrent (torrents) or SABnzbd (usenet). It is **NOT** in the in-hunt source loop — a torrent/NZB downloads over minutes-to-hours, which doesn't fit `RomSource.download_file`'s 5-min synchronous contract.

Flow:
- **`DownloadClient` ABC** (`download_clients/base.py`) + `download_clients/registry.py` (mirrors `sources/registry`). Loaded via the `download_client` branch in `extension_loader.py`; enabled flag is `download_client_{id}_enabled`. qBittorrent **5.x** uses `start`/`stop` + add-param `stopped` (the client detects WebAPI version); LAN auth-bypass means username/password are optional.
- **`download_clients/selection.py`** (LOCAL, pure, tested): `classify_files` → single / **multidisc (keep all discs)** / **pack (keep only matching files via qBit `filePrio`)** / none; reuses `hunter._file_score`. `looks_like_pack` skips usenet multi-ROM packs (no per-file selection). `release_is_relevant` = title-level filter.
- **`external_hunt.submit_external`** runs at hunt **HTTP-source exhaustion** (gated on a configured+enabled client) — one Prowlarr search, best acceptable release → `client.submit` (qBit add-paused / SAB addurl) → records an **`ExternalDownload`** + a `downloading` `Download` card, and sets `WantedGame.status = awaiting_external`. **Makes NO RA calls** (the hunt passes in `ra_hashes`/stems/terms, stored in `ExternalDownload.match_data`).
- **`scheduler.run_poll_external` → `external_hunt.poll_active`** runs every scheduler tick **while any job is non-terminal** (not a daily slot). It advances torrent file-selection once metadata arrives, updates the Download card's progress, and on completion **ingests + RA-verifies** (`ra_hash_or_fallback` → `lookup_hash` → `_verified_game_id` against the stored accepted hashes — at most one RA call per completed file). Verified → Download promoted to `pending_approval`, Wanted→verified, LibraryEntry (non-review). Bad-hash/fail/stall → transient Download deleted, `ExternalDownload`→failed, Wanted→exhausted, qBit torrent removed (deleteFiles on bad hash). **Deleted-card guard:** if the linked `Download` row is gone at ingest time (the user deleted its progress card mid-download), `_ingest` does NOT recreate it — it treats the job as cancelled (removes the just-staged file, `_fail(..., delete_files=True)`), so a user cancel can't be silently undone by the poller re-materialising the card.
- **Deployment caveat:** ingest moves the file from the client's completed path into rom-finder's staging dir, so the **rom-finder container must be able to read qBittorrent/SABnzbd's download folder** (a shared mount). If it can't, ingest logs a clear warning and the job fails. `/api/status.external` + the `external` `/api/changes` scope surface state. Settings: `sched_poll_external_enabled`, `external_download_stall_minutes`, + the extension's `ext_download_client_*` (Prowlarr/qBit/SAB URLs+keys).

## Cover Sources (`cover_sources/`)

Each source extends `BaseCoverSource`:
- `source_id: str`
- `name: str`
- `requires_api_key: bool`
- `async fetch_cover(ra_game_id, title, system, config) → bytes | None`

Sources tried in priority order (drag-to-reorder in Settings). First returning bytes wins.  
Implemented: `retroachievements` (uses RA game ID), `steamgriddb` (title search, requires API key).

**`ra_game_id = None` guard**: The RA cover source must check `if not ra_game_id: return None` — otherwise makes a request with `?i=None`.

---

## Scheduler (`scheduler.py`)

Three daily tasks configured via `/scheduler`:

| Task | What it does |
|---|---|
| **Library scan** | Walk `download_dir` → insert new ROMs → hash → fetch cover → RA verify |
| **Hash check** | Backfill `hashed_at`; clear stale hashes (mtime > hashed_at); hash un-hashed entries |
| **RA autodiscover** | `get_game_list()` per tracked system; add games with new achievement sets to Wanted |
| **Event sync** (`eventsync`, ~05:30) | Re-check each auto-sync `GoalEvent` for newly-added achievements (`events.sync_all_auto`); one RA call per event |
| **CHD format check** (`chdcheck`, ~04:30, **opt-in**) | Flag (and, with `chdman`, re-encode) owned CHDs on the RA-incompatible Zstandard codec (`chd_format.run_chd_check`). **Double-gated**: its own `sched_chdcheck_enabled` AND the master `chd_format_check_enabled` flag (both default off) — the loop skips it unless the master flag is on. |

`scheduler_loop()` wakes every 60s, checks `_should_run()` per enabled task against configured local time. Started as `asyncio.create_task()` in `lifespan`. **The `chdcheck` task is additionally skipped unless `chd_format_check_enabled` is true.**

**Run Now behaviour**: `POST /scheduler/run/{task_id}` is synchronous — HTTP response held until task completes (intentional for self-hosted, no proxy timeouts). UI shows spinner via `hx-indicator`; "Last run" timestamp updates via HTMX OOB swap after completion.
