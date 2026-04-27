# ROM Finder — Task Backlog

Ordered by value/effort ratio. Check items off as they land; move completed items to `tasks/done.md`.

---

## High priority

- [ ] **Tests: core unit coverage** — `hasher.py` hash functions, `title_utils.py` search variations, `vimm.py` name filter + URL logic, `collection.py` `_build_collection` merging logic. See `tests/README.md` for setup.
- [ ] **Hash export** — Generate a CSV/JSON of verified ROMs (title, system, hash, RA game ID) for importing into emulator frontends or sharing. Endpoint: `GET /collection/export/hashes`. Format toggle: CSV vs JSON.
- [ ] **Bulk RA verify after hash check** — After the scheduler's hash-check task runs, automatically queue RA verification for any newly-hashed entries that aren't yet verified. Currently the user has to manually click "Verify hashes" on the Collection page.

## Medium priority

- [ ] **Import existing collection** — Point at a folder of already-owned ROMs and bulk-import + hash + RA verify in one pass. Reuses the scanner from `run_scan()` but scans a user-supplied path instead of `download_dir`.
- [ ] **RA progress tracking** — Show achievement completion % per game on collection cards. Requires RA user progress API (`GetUserProgress`). Gate on RA credentials being configured.
- [ ] **Notification on autodiscover** — When the scheduler's autodiscover task adds new games to Wanted, surface a count badge in the sidebar nav on the Wanted link.
- [ ] **Emulator launch** — "Play" button on collection cards that launches a configured emulator with the ROM path. Settings: emulator path + per-system command template.
- [ ] **Vimm headless download** — Use Playwright to bypass Vimm's JS bot protection for automated downloads. Heavy dependency (~300MB browser binary); evaluate whether Docker image size increase is acceptable.

## Low priority / nice to have

- [ ] **Wanted page: URL-persisted filters** — Currently filter state (system, status, search) is lost on navigation. Persist via URL query params like the Collection page does.
- [ ] **Collection page: sort options** — Currently sorted by added_at desc. Add: A–Z, system, RA match status.
- [ ] **Cover art: custom upload** — Let users upload a PNG to replace the auto-fetched cover for any game.
- [ ] **Stale download cleanup** — Auto-delete `Download` records stuck in `downloading` or `hashing` state after a configurable timeout (e.g. 24h). Useful after server restarts mid-download.
- [ ] **More ROM sources** — Vimm (pending headless solution above), ROMsFun, WowROMs. Stubs already exist in `app/services/sources/`.
- [ ] **Settings: test cover source** — "Test" button per cover source that fetches a sample cover and previews it inline.
- [ ] **Node.js 24 action migration** — GitHub Actions is deprecating Node.js 20 in June 2026. Update `actions/checkout`, `docker/build-push-action`, `docker/login-action`, `docker/metadata-action` to Node.js 24 compatible versions.

---

## Completed

- [x] Collection page (cards + list, filters, status vocabulary)
- [x] Wanted page (RA search, per-game source search)
- [x] Download pipeline (queue, progress, hash, RA verify, approve/reject)
- [x] Scheduler (library scan, hash check, autodiscover — all with Run Now)
- [x] Cover art system (RA + SteamGridDB sources, bulk fetch, per-card refresh)
- [x] Vimm source (search, file browse, DMCA detection, correct POST download mechanism)
- [x] Review directory toggle (optional staging; auto-import when disabled)
- [x] Sidebar nav fix (/wanted link, correct active-link detection)
- [x] Scheduler UX (loading spinner on Run Now, OOB Last Run timestamp update)
