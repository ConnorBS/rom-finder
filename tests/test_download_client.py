"""extensions/download_client.py — Prowlarr search parsing/filtering + helpers.
Loaded by path (like test_vimm_source); httpx is faked, no network."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "extensions" / "download_client.py"
_spec = importlib.util.spec_from_file_location("romfinder_ext_download_client", _PATH)
dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dc)


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


class _FakeClient:
    def __init__(self, payload):
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        return _Resp(self._p)


def _make(monkeypatch, payload):
    monkeypatch.setattr(dc.httpx, "AsyncClient", lambda *a, **k: _FakeClient(payload))
    c = dc.TorrentUsenetClient()
    c.configure({"prowlarr_url": "http://p:9696", "prowlarr_api_key": "k",
                 "qbit_url": "http://q:8080", "sab_url": "http://s:8080", "sab_api_key": "sk"})
    return c


def test_search_maps_and_filters_by_protocol(monkeypatch):
    payload = [
        {"title": "Sly Cooper (USA) PS2", "downloadUrl": "http://x/a.torrent",
         "infoHash": "ABCDEF", "size": 700, "seeders": 5, "protocol": "torrent", "indexer": "idx1"},
        {"title": "Sly Cooper (USA)", "downloadUrl": "http://x/a.nzb",
         "size": 700, "protocol": "usenet", "indexer": "idx2"},
        {"title": "Weird", "downloadUrl": "x", "protocol": "ftp", "indexer": "i"},
    ]
    c = _make(monkeypatch, payload)
    res = asyncio.run(c.search("sly cooper", "PlayStation 2"))
    assert len(res) == 2                              # torrent + usenet kept; ftp dropped
    t = [r for r in res if r["protocol"] == "torrent"][0]
    assert t["info_hash"] == "abcdef"                 # lower-cased
    assert t["download_url"].endswith("a.torrent") and t["seeders"] == 5


def test_search_disabled_without_creds(monkeypatch):
    c = dc.TorrentUsenetClient()
    c.configure({})                                   # no prowlarr url/key
    assert asyncio.run(c.search("anything")) == []


def test_protocols_reflect_configured_backends():
    c = dc.TorrentUsenetClient()
    c.configure({"qbit_url": "http://q:8080"})        # torrent only (no sab key)
    assert c.protocols == {"torrent"}
    c.configure({"sab_url": "http://s:8080", "sab_api_key": "k"})  # usenet only
    assert c.protocols == {"usenet"}


def test_infohash_from_magnet():
    h = dc._infohash_from_magnet("magnet:?xt=urn:btih:0123456789ABCDEF0123456789abcdef01234567&dn=x")
    assert h == "0123456789abcdef0123456789abcdef01234567"
