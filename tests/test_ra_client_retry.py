"""RAClient._get_with_retry: the dashboard history pull (get_achievements_earned_between
+ get_user_completion_progress) now retries ONCE on a 429 instead of letting a single
transient rate-limit abort the whole refresh and leave the mirror stale."""
import asyncio

import httpx
import pytest

from app.services import ra_client as ra_client_mod
from app.services.ra_client import RAClient
from app.services.sources.errors import SourceRateLimitError


class _FakeResp:
    def __init__(self, status, json_data=None, headers=None):
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _FakeClient:
    """Stands in for httpx.AsyncClient — hands back queued responses in order."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    # Don't actually sleep on Retry-After or block on the 2 req/s limiter in tests.
    async def _instant(*a, **k):
        return None
    monkeypatch.setattr(ra_client_mod.asyncio, "sleep", _instant)
    monkeypatch.setattr(ra_client_mod._limiter, "wait", _instant)


def test_get_with_retry_recovers_from_single_429():
    ra = RAClient("u", "k")
    client = _FakeClient([
        _FakeResp(429, headers={"Retry-After": "0"}),
        _FakeResp(200, json_data=[{"AchievementID": 1}]),
    ])
    resp = asyncio.run(ra._get_with_retry(client, "http://x", {}))
    assert client.calls == 2                       # retried once
    assert resp.json() == [{"AchievementID": 1}]   # second (200) response used


def test_get_with_retry_raises_on_persistent_429():
    ra = RAClient("u", "k")
    client = _FakeClient([
        _FakeResp(429, headers={"Retry-After": "0"}),
        _FakeResp(429, headers={"Retry-After": "0"}),
    ])
    with pytest.raises(SourceRateLimitError):
        asyncio.run(ra._get_with_retry(client, "http://x", {}))
    assert client.calls == 2                        # tried twice, then gave up


def test_get_with_retry_passes_through_success():
    ra = RAClient("u", "k")
    client = _FakeClient([_FakeResp(200, json_data={"Total": 0, "Results": []})])
    resp = asyncio.run(ra._get_with_retry(client, "http://x", {}))
    assert client.calls == 1
    assert resp.json()["Total"] == 0
