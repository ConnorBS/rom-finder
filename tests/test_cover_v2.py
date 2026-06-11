"""V2 box-art cover source: registered, guards on missing id/key, and returns the
downloaded bytes when V2 yields an imageBoxArtUrl. Network mocked."""
import asyncio


def test_registered():
    from app.services import cover_sources as reg
    assert reg.get("ra_v2_boxart") is not None


def test_guards_missing_id_and_key():
    from app.services.cover_sources.ra_v2_boxart import RAV2BoxArtCoverSource
    src = RAV2BoxArtCoverSource()
    assert asyncio.run(src.fetch_cover(0, "T", "NES", {"ra_api_key": "k"})) is None       # no id
    assert asyncio.run(src.fetch_cover(9, "T", "NES", {})) is None                          # no key


def test_happy_path_returns_bytes(monkeypatch):
    from app.services.cover_sources import ra_v2_boxart
    from app.services.ra_client_v2 import RAClientV2

    async def fake_game(self, game_id, include=""):
        return {"data": {"attributes": {"imageBoxArtUrl": "https://media.ra/boxart/9.png"}}}
    monkeypatch.setattr(RAClientV2, "get_game", fake_game)

    class _Img:
        status_code = 200
        content = b"PNGBYTES"

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Img()
    monkeypatch.setattr(ra_v2_boxart.httpx, "AsyncClient", _FakeClient)

    out = asyncio.run(ra_v2_boxart.RAV2BoxArtCoverSource().fetch_cover(9, "T", "NES", {"ra_api_key": "k"}))
    assert out == b"PNGBYTES"
