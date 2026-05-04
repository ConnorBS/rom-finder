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
- `romsfun` (`extensions/romsfun.py`) — CSS-hidden CDN URL in HTML, one file per game, refreshed at download time
- `vimm` (`extensions/vimm.py`) — requires Playwright for JS challenge

## Vimm Gotcha

Vimm blocks automated downloads with a JS challenge. Vault ID shown in URLs ≠ the `mediaId` in the download form. DMCA'd games have no `dl_form`. See project memory for details.
