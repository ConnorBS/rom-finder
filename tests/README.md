# Tests

Run from the repo root:

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

## Coverage targets

| File | Test file | What's covered |
|------|-----------|----------------|
| `app/services/hasher.py` | `test_hasher.py` | Hash functions per system, zip extraction |
| `app/services/title_utils.py` | `test_title_utils.py` | Search query generation |
| `app/services/sources/vimm.py` | `test_vimm_source.py` | Name filter logic, URL generation, mediaId extraction |
| `app/routers/collection.py` | `test_collection.py` | `_build_collection` merge logic, status vocabulary |
| `app/services/scheduler.py` | `test_scheduler.py` | `_should_run` timing logic |

## Philosophy

- Unit tests only (no DB, no network). Async tests use `pytest-asyncio`.
- Test edge cases first: None values, empty inputs, mismatched types.
- When a bug is fixed, add a regression test named after the scenario.
