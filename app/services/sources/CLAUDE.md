# app/services/sources/ — ROM Download Sources

## Design Goal

Extensions should make it effortless for the user to download a ROM — search returns games, clicking a result immediately shows the file with a Download button. No multi-step browsing required.

The Archive.org source is the historical exception: it searches collections that may contain many unrelated files, so it has a "Browse files" step. Every other source should avoid this — return one result per game, fetch the direct CDN link in `get_files()`, and let the user click Download immediately.

---

## The Three Methods

### `search(query, system) -> list[dict]`

Search the site and return one dict per matching game. Each dict needs:
- `identifier` — a stable key you'll use later in `get_files()` (e.g. `"{system}/{slug}"`)
- `title` — display name
- `source_id` — your `source_id` string
- `description` — optional; system name, region, etc.

**Do not return collections or archives with many files.** Return individual games.

### `get_files(identifier, name_filter) -> list[dict]`

Given an identifier from `search()`, fetch the game page and extract the download URL. Return **one dict per downloadable file**. Each dict needs:
- `name` — the filename (e.g. `"Super Mario Bros (USA).nes"` or `"game.zip"`)
- `identifier` — the download key; for direct-CDN sources this is the CDN URL or the path needed to generate one
- `source_id` — your `source_id` string
- `size` — bytes, or 0 if unknown

For most sources there is exactly one file per game — return a single-item list. This makes the file list auto-expand with a single Download button on the Wanted page (no "Browse files" click needed).

### `get_download_url(identifier, filename) -> str`

Convert the file identifier into the URL passed to `download_file()`. If the identifier IS the URL (e.g. a CDN URL stored directly), just return it. If the identifier is a path fragment, prefix the base URL.

### `download_file(url, dest, progress_callback)` — override when needed

The base class streams `url` with httpx. Override this when the real CDN URL must be fetched at download time (e.g. WowROMs and ROMsFun generate signed tokens that expire, so `get_files()` stores the mirror page URL and `download_file()` fetches a fresh token before streaming).

---

## Extension Pattern (typical direct-download source)

```
search()
  → scrape search results page
  → return [{identifier: "sys/slug", title: "...", source_id: "...", description: "..."}]

get_files(identifier)
  → fetch game page at /roms/{sys}/{slug}.html
  → find the download button href / AJAX URL
  → (optional) fetch one more page to get the signed CDN URL + real filename
  → return [{"name": "Game.zip", "identifier": cdn_url, "source_id": "...", "size": 0}]

get_download_url(identifier)
  → return identifier  # already is the CDN URL

# base class download_file() streams it — done
```

Or, for token-based CDNs:

```
get_files()        → returns mirror page URL as identifier
get_download_url() → returns full mirror page URL
download_file()    → override: re-fetches mirror for fresh token, streams CDN
```

---

## Existing Sources

- `archive_org` (`app/services/sources/archive.py`) — built-in; Archive.org No-Intro/Redump collections. Has the "Browse files" step because collections contain many ROMs.
- `cdromance` (`extensions/cdromance.py`) — AJAX-based CDN, one file per game, no Playwright
- `wowroms` (`extensions/wowroms.py`) — token-based CDN (`k`=ms timestamp, `t`=md5(k)), one file per game
- `romsfun` (`extensions/romsfun.py`) — WordPress AJAX endpoint (`action=k_get_download`, `Referer`=mirror page URL) returns fresh signed CDN URL; HTML link is stale placeholder. `get_files()` and `download_file()` both call AJAX. **A game page can list more than one ROM** (e.g. a USA dump *and* a Europe dump at `/download/{slug}-{id}/1`, `/2`) — only one may be RA's accepted hash, so `get_files()` enumerates every per-file path (`_file_paths()`) off the landing page and AJAX-resolves each (dedup by CDN filename; the per-mirror `Referer` is what makes `/1` and `/2` resolve to different files). Returning only the first file made hunts exhaust after one (wrong-region) bad-hash attempt.
- `vimm` (`extensions/vimm.py`) — requires Playwright for JS challenge

## Vimm Gotcha

Vimm blocks automated downloads with a JS challenge. Vault ID shown in URLs ≠ the `mediaId` in the download form. DMCA'd games have no `dl_form`. See project memory for details.

**Search-result vault id must be a complete path segment** (`_VAULT_ID_RE = /vault/(\d+)(?=[/?#]|$)`). The old loose `/vault/(\d+)` matched the digit prefix of console-category links — `/vault/32X` → "32", `/vault/3DO` → "3" — surfacing bogus "Sega 32X" / "3DO" rows alongside the real game.

**Playwright is bundled (Phase 7):** the Dockerfile runs `playwright install --with-deps chromium`, so Vimm downloads work in the deployed image (they used to always fail with "Playwright is not installed"). Vimm imports Playwright lazily (inside its methods), so the extension and tests load fine without it; only an actual download needs the browser. Image cost: ~400MB (chromium only).

---

## Error handling (Phase 4)

`errors.py` defines a typed taxonomy — **never `return []` / `except: pass` on failure**, raise the right one so the router can surface it (results partial + `/logs`) and callers can back off:
`SourceError` (base) → `SourceNetworkError` (timeout / 5xx / malformed), `SourceForbiddenError` (403 — bot block or stale signed token), `SourceRateLimitError` (429, carries `retry_after`), `SourceNotFoundError` (404), `SourceBadHashError`. `classify_status(code, ...)` maps an HTTP status to the right one.

Routers query sources **per-source** (`games.search`, `api.api_search`): one source failing must not wipe out the others' results, and the failure is logged/surfaced, not swallowed.

## `download_file` contract (base class)

The base `download_file` now: uses a real `httpx.Timeout` (no more `timeout=None` hangs on dead mirrors); streams to `dest.name + ".part"` then `os.replace` (atomic — a failed/partial download never leaves a file the scanner treats as a ROM); verifies bytes-written == `Content-Length` when present (catches truncated/short-body responses); and raises the typed errors above on HTTP ≥ 400. Token-CDN overrides should preserve this behavior (raise `SourceForbiddenError` on 403).

## Archive member selection

`extract_rom_from_zip(zip, prefer_name=...)` and `hash_rom(..., prefer_name=...)` prefer the archive member whose stem matches the expected ROM name (so multi-ROM zips don't grab the wrong game), falling back to the largest member and logging a WARNING when the archive is ambiguous. Pass `prefer_name` (the download's `game_title` / `file_name`) from the call site.
