# app/services/sources/ — ROM Download Sources

## Pattern

Each source file exports a class extending `BaseSource`:

```python
class MySource(BaseSource):
    source_id = "my_source"
    name = "My Source"
    available = True  # False = "coming soon" in UI, endpoint disabled

    async def search(self, query: str, system: str) -> list[dict]:
        ...
```

Register in the sources registry so it appears in settings and search UI.

## Current Sources

- `archive_org` — Archive.org No-Intro/Redump search (implemented)
- `vimm` — stub
- `romsfun` — stub
- `wowroms` — stub

## Vimm Gotcha

Vimm blocks automated downloads with a JS challenge. Vault ID shown in URLs ≠ the `mediaId` used in the actual download form. DMCA'd games have no `dl_form` at all. See project memory for details.
