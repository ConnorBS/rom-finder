"""RA V2 probe (/api/diag/ra-v2) — confirms the diagnostic parses a JSON:API event
payload (attributes + relationships + included award tiers) and degrades cleanly with
no creds / a non-JSON (Cloudflare) body. Network is mocked — this guards the parsing
shape, not live reachability (that's what the probe itself is for)."""
from sqlmodel import Session

from app.db.database import engine


class _FakeResp:
    def __init__(self, status=200, json_data=None, text="", ct="application/json"):
        self.status_code = status
        self._j = json_data
        self.text = text
        self.headers = {"content-type": ct}

    def json(self):
        if self._j is None:
            raise ValueError("no json")
        return self._j


def _seed_creds():
    from app.services import settings as app_settings
    with Session(engine) as s:
        app_settings.set(s, "ra_username", "u")
        app_settings.set(s, "ra_api_key", "k")


def test_diag_ra_v2_no_credentials(client):
    out = client.get("/api/diag/ra-v2?event=196").json()
    assert out["has_credentials"] is False and "error" in out


def test_diag_ra_v2_parses_event_and_awards(client, monkeypatch):
    from app.services.ra_client_v2 import RAClientV2
    _seed_creds()

    async def fake_get(self, path, params=None):
        if path.startswith("/events/"):
            return _FakeResp(json_data={
                "data": {"type": "events", "id": "196",
                         "attributes": {"title": "AotW 2026", "badgeUrl": "b", "achievementsPublished": 64},
                         "relationships": {"awards": {"data": []}}},
                "included": [
                    {"type": "user-awards", "attributes": {"kind": "bronze", "title": "Bronze"}},
                    {"type": "user-awards", "attributes": {"kind": "gold", "title": "Gold"}},
                ],
            })
        return _FakeResp(json_data={
            "data": {"type": "achievements", "id": "609272",
                     "attributes": {"title": "X", "points": 5, "pointsWeighted": 25, "badgeUrl": "ab"},
                     "relationships": {"games": {"data": [{"type": "games", "id": "10003"}]}}},
            "included": [{"type": "games", "id": "10003", "attributes": {"title": "Gex 3"}}],
        })
    monkeypatch.setattr(RAClientV2, "get", fake_get)

    out = client.get("/api/diag/ra-v2?event=196&achievement=609272").json()
    assert out["has_credentials"] is True
    ev = out["event"]
    assert ev["status"] == 200
    assert ev["attributes"]["title"] == "AotW 2026"
    assert "awards" in ev["relationships"]
    assert ev["included_types"] == ["user-awards"]
    ach = out["achievement"]
    assert ach["attributes"]["pointsWeighted"] == 25
    assert "games" in ach["relationships"]
    assert ach["included_types"] == ["games"]


def test_diag_ra_v2_handles_non_json_body(client, monkeypatch):
    from app.services.ra_client_v2 import RAClientV2
    _seed_creds()

    async def fake_get(self, path, params=None):
        return _FakeResp(status=403, json_data=None, text="<html>Cloudflare challenge</html>", ct="text/html")
    monkeypatch.setattr(RAClientV2, "get", fake_get)

    ev = client.get("/api/diag/ra-v2?event=196").json()["event"]
    assert ev["status"] == 403 and "Cloudflare" in ev["body_snippet"]
