"""Phase 0.5: agent-observable diagnostics endpoints."""


def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["app"] == "rom-finder"
    assert "version" in data
    # Each major section present and not an error dict.
    assert data["rahasher"].get("error") is None
    assert "available" in data["rahasher"]
    assert data["db"]["library_total"] == 0
    assert data["db"]["no_ra"] == 0
    assert set(data["scheduler"]) == {"scan", "hash", "autodiscover"}
    assert "verify" in data
    assert "recent_errors" in data and "count" in data["recent_errors"]


def test_logs_returns_json_list(client):
    r = client.get("/api/logs?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)


def test_startup_logged_to_db(client):
    # Lifespan emits a "ROM Finder started" info log; it must be HTTP-visible.
    r = client.get("/api/logs?category=system&limit=50")
    assert r.status_code == 200
    messages = [e["message"] for e in r.json()]
    assert any("ROM Finder started" in m for m in messages)


def test_logs_level_filter(client):
    r = client.get("/api/logs?level=info&limit=50")
    assert r.status_code == 200
    assert all(e["level"] == "info" for e in r.json())
