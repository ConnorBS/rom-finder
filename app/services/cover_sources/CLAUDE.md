# app/services/cover_sources/ — Cover Art Sources

## Pattern

Each source extends `BaseCoverSource`:

```python
class MySource(BaseCoverSource):
    source_id = "my_source"
    name = "My Source"
    requires_api_key = False

    async def fetch_cover(self, ra_game_id, title, system, config) -> bytes | None:
        ...
```

Register in `registry.py`. Add `cover_source_{id}_enabled` (and `cover_source_{id}_api_key` if needed) to `DEFAULT_SETTINGS` in `main.py`.

Sources are tried in user-configured priority order. First returning `bytes` wins.

## Current Sources

- `retroachievements` — uses `ra_game_id` to fetch the game ICON from RA's V1 CDN. **Guard**: `if not ra_game_id: return None`.
- `ra_v2_boxart` — full **box art** from RA **V2** (`/games/{id}` → `imageBoxArtUrl`); uses the RA API key (Bearer). Higher quality than the V1 icon; off by default (`cover_source_ra_v2_boxart_enabled`). Falls through to the next source if V2 is unreachable.
- `steamgriddb` — title-based search; requires API key.

## Cover Filename Convention

- `{ra_game_id}.png` when RA ID is known
- `lib_{library_id}.png` for LibraryEntry with no RA ID
