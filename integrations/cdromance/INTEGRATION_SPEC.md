# CDRomance Integration Specification

## Overview

**CDRomance** (https://cdromance.org) is a ROM/ISO download website specializing in fan-made mods, translations, undubs, and retro game ISOs. This document specifies how to integrate CDRomance as a new ROM source in the rom-finder application.

---

## Source Classification

| Property | Value |
|---|---|
| `source_id` | `cdromance` |
| `name` | `CDRomance` |
| `available` | `True` (requires headless browser for downloads) |
| `base_url` | `https://cdromance.org` |
| `requires_browser` | `True` (search is HTTP-based; downloads require Playwright) |

---

## Supported Platforms

CDRomance covers a wide range of platforms, organized into categories in the site navigation:

### SEGA
- Dreamcast (`dc-iso`)
- Saturn (`sega_saturn_isos`)
- SEGA CD (`sega_cd_isos`)
- 32X (`sega_32x_roms`)
- Genesis (`sega_genesis_roms`)
- Master System (`sms_roms`)
- Game Gear (`game-gear`)

### SONY
- PlayStation (`psx-iso`)
- PlayStation 2 (`ps2-iso`)
- PSP (`psp`, including PSP Themes and Eboots)
- VITA (`vita`)

### NINTENDO
- Wii (`wii-iso`)
- GameCube (`gamecube`)
- N64 (`n64-roms`)
- SNES (`snes-rom`)
- NES (`nes-roms`)
- Famicom Disk System (`famicom_disk_system`)
- Nintendo DS (`nds-roms`)
- Game Boy Advance (`gba-roms`)
- Game Boy Color (`gameboy-color-roms`)
- Game Boy (`gameboy-roms`)

### NEC
- TurboGrafx-16 (`turbografx-16`)
- TurboGrafx-CD (`turbografx-cd`)
- PC-FX (`pc-fx`)

### SNK
- Neo Geo Pocket Color (`neo-geo-pocket`)
- Neo Geo CD (`neo-geo-cd`)

### PC & OTHER
- Windows (`windows`)
- ScummVM (`scummvm`)
- MS-DOS (`msdos`)
- 3DO (`3do-iso`)
- WonderSwan (`wonderswan`)
- MSX (`msx-roms`)

### Special Categories
- Translations (`translations`) — English-patched ROMs/ISOs
- Undubs (`translations?language=undub`) — Region-restored content
- Romhacks (`romhacks`) — Community-modified ROMs
- DLC/Extras — PSP DLC, VITA DLC, BIOS Files, SBI Files

---

## System Name Mapping

Map RA/system display names to CDRomance platform slugs:

```python
_SYSTEM_MAP: dict[str, str] = {
    # Nintendo
    "NES": "nes-roms",
    "SNES": "snes-rom",
    "Nintendo 64": "n64-roms",
    "Game Boy": "gameboy-roms",
    "Game Boy Advance": "gba-roms",
    "Game Boy Color": "gameboy-color-roms",
    "Nintendo DS": "nds-roms",
    "GameCube": "gamecube",
    "Wii": "wii-iso",
    "Virtual Boy": "nintendo-virtual-boy",

    # Sega
    "Sega Genesis / Mega Drive": "sega_genesis_roms",
    "Sega CD": "sega_cd_isos",
    "Sega 32X": "sega_32x_roms",
    "Saturn": "sega_saturn_isos",
    "Dreamcast": "dc-iso",
    "Master System": "sms_roms",
    "Game Gear": "game-gear",

    # Sony
    "PlayStation": "psx-iso",
    "PlayStation 2": "ps2-iso",
    "PlayStation Portable": "psp",
    "PlayStation Vita": "vita",

    # NEC
    "PC Engine / TurboGrafx-16": "turbografx-16",
    "PC Engine CD": "turbografx-cd",

    # SNK
    "Neo Geo Pocket": "neo-geo-pocket",
    "Neo Geo CD": "neo-geo-cd",

    # Other
    "3DO Interactive Multiplayer": "3do-iso",
    "WonderSwan": "wonderswan",
}
```

---

## Search Implementation

### Search URL Patterns

CDRomance uses WordPress-based search. The search endpoint accepts a `s` query parameter:

```
# Global search
https://cdromance.org/?s={query}

# Platform-scoped search (append platform slug to base)
https://cdromance.org/{platform_slug}/?s={query}
```

### Search Response Parsing

Search results are returned as HTML articles. Each result card contains:
- **Title**: Game name (in `<h2>` or link text)
- **Platform badge**: Console name (e.g., "PS2", "NES", "GBA")
- **Date**: Publication date
- **URL**: Permalink to the game page
- **Cover image**: Game coverart thumbnail

### Game Page Structure

Each game page (e.g., `/ps2-iso/sonic-mega-collection-plus-europe/`) contains:

1. **Game Information Block**:
   - Game Name, Region, Console, Release Date, Genre, Publisher
   - Languages supported
   - Image Format (ISO, ZIP, etc.)
   - Game ID (e.g., SLES-52998 for PS2)
   - Download count, User score

2. **Description**: Game description and feature list

3. **Screenshots**: Gallery of game screenshots

4. **Download Section**:
   - Filename and Filesize displayed
   - "Download" button (triggers download flow)

### Search Implementation Steps

```python
async def search(self, query: str, system: str = "") -> list[dict]:
    # 1. Determine platform slug from system (if provided)
    # 2. Build search URL (platform-scoped or global)
    # 3. Fetch with httpx (standard HTTP, no JS needed for search)
    # 4. Parse with BeautifulSoup
    # 5. Extract game title, URL, platform, and region from each article
    # 6. Return list of result dicts
```

**Identifier format**: Use the URL path as the identifier (e.g., `ps2-iso/sonic-mega-collection-plus-europe`). This is stable and unique.

---

## File Listing (`get_files`)

### Game Page Parsing

Each game page has a single download entry. The file info is extracted from:

1. **Filename**: Often shown in a download table or inferred from the page title
2. **Filesize**: Displayed next to the filename (e.g., "1.5 GB", "256 MB")
3. **Format**: ISO, ZIP, 7Z, etc. (shown in game info block)

### Size Parsing

```python
def _parse_size(size_str: str) -> int:
    """Parse human-readable size to bytes."""
    size_str = size_str.strip().upper()
    match = re.match(r"([\d.]+)\s*(KB|MB|GB|TB)", size_str)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2)
    multipliers = {"KB": 1024**1, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * multipliers[unit])
```

---

## Download Implementation

### Download Flow

CDRomance uses a **button-click triggered download** that requires a headless browser:

1. Navigate to the game page URL
2. Locate the download button (typically has text "Download" or class containing "download")
3. Click the button
4. The site shows "Download Requested..." and initiates the file transfer
5. Browser handles the redirect to the actual download URL

### Playwright Download Handler

```python
async def download_file(self, url: str, dest: Path, progress_callback=None):
    """Download via headless browser — CDRomance requires JS interaction."""
    async with _get_cdromance_lock():  # One download at a time
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            await page.goto(url, timeout=30000, wait_until="domcontentloaded")

            # Find and click the download button
            dl_button = page.locator(
                "button:has-text('Download'), a:has-text('Download'), "
                "input[type='submit']:has-text('Download')"
            ).first

            async with page.expect_download(timeout=120000) as dl_info:
                await dl_button.click()

            download = await dl_info.value
            await download.save_as(str(dest))

            await browser.close()
```

### Concurrency Lock

Like VIMM, CDRomance should use a module-level `asyncio.Lock` to prevent concurrent downloads:

```python
_CDR_OMANCE_LOCK: asyncio.Lock | None = None

def _get_cdromance_lock() -> asyncio.Lock:
    global _CDR_OMANCE_LOCK
    if _CDR_OMANCE_LOCK is None:
        _CDR_OMANCE_LOCK = asyncio.Lock()
    return _CDR_OMANCE_LOCK
```

---

## Registration

In `app/services/sources/__init__.py`:

```python
from .cdromance import CdromanceSource

# ... existing registrations ...
register(CdromanceSource())
```

---

## UI Considerations

### Source Badge
- Display name: **CDRomance**
- Icon: Use the CDRomance phoenix logo or a generic download icon
- Color accent: Consider matching the site's theme (dark/red)

### Special Content Indicators
CDRomance has unique content types that should be flagged in search results:
- **🌐 Translation** — English-patched ROMs
- **🔄 Undub** — Region-restored content
- **🔧 Romhack** — Community modifications
- **📦 DLC** — Downloadable content

These can be detected from the URL path:
- `/ps2-iso/...` with "english-patched" in slug → Translation
- `/translations/...` → Translation
- `/romhacks/...` → Romhack
- PSP/VITA DLC URLs → DLC

---

## Error Handling

| Scenario | Handling |
|---|---|
| Site unreachable | Return empty results, log warning |
| No download button found | Raise `ValueError` with page URL for debugging |
| Download timeout | Raise with message suggesting manual retry |
| DMCA-removed content | Check for "404" or "removed" text on page, return empty file list |
| Search returns no results | Return empty list, optionally retry with shortened query |

---

## Rate Limiting & Ethics

- **Respect `robots.txt`**: Check CDRomance's crawl policy
- **Add delay between requests**: 1-2 second delay between search and page fetch
- **User-Agent**: Use a descriptive UA identifying the rom-finder app
- **One concurrent download**: Enforced by asyncio lock
- **Cache search results**: If implementing caching, TTL of 24h recommended

---

## Testing Checklist

- [ ] Search for a known PS2 game (e.g., "Sonic Mega Collection Plus")
- [ ] Search with system filter (e.g., query="sonic", system="PlayStation 2")
- [ ] Search without system filter (global search)
- [ ] Verify game page parsing extracts correct filesize
- [ ] Test download flow with Playwright (small file first)
- [ ] Test concurrent download queuing (lock behavior)
- [ ] Test error handling for removed/unavailable games
- [ ] Test translation/romhack URL detection
- [ ] Verify source appears in Settings → Sources list
- [ ] Verify source works in Wanted list search

---

## Implementation Priority

1. **Phase 1**: Basic search + HTTP-based file listing
2. **Phase 2**: Playwright download handler
3. **Phase 3**: Special content type detection (translations, romhacks)
4. **Phase 4**: Cover art extraction from game pages
5. **Phase 5**: Cache layer for search results

---

## References

- **Site**: https://cdromance.org
- **Example game page**: https://cdromance.org/ps2-iso/sonic-mega-collection-plus-europe/
- **Platform listing**: https://cdromance.org/platforms/
- **Base source class**: `app/services/sources/base.py`
- **Reference implementation**: `app/services/sources/vimm.py` (Playwright downloads)

</contents>