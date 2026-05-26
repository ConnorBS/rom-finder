"""Phase 9b: hash export."""


def _seed(client):
    # Use the running app's DB via a direct insert through the engine.
    from sqlmodel import Session
    from app.db.database import engine
    from app.db.models import LibraryEntry
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Sonic", system="Genesis", file_name="sonic.md",
                           file_path="/r/sonic.md", file_hash="h1", ra_matched=True,
                           hash_verified=True, ra_game_id=7))
        s.add(LibraryEntry(game_title="Nomatch", system="NES", file_name="x.nes",
                           file_path="/r/x.nes", file_hash="h2", ra_matched=False))
        s.commit()


def test_export_csv(client):
    _seed(client)
    r = client.get("/export/hashes?format=csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    assert "game_title,system,file_name,file_hash,ra_game_id,ra_matched,hash_verified" in body
    assert "Sonic" in body and "Nomatch" in body


def test_export_json(client):
    _seed(client)
    r = client.get("/export/hashes?format=json")
    assert r.status_code == 200
    titles = {row["game_title"] for row in r.json()}
    assert {"Sonic", "Nomatch"} <= titles


def test_export_verified_only(client):
    _seed(client)
    r = client.get("/export/hashes?format=json&verified_only=true")
    titles = {row["game_title"] for row in r.json()}
    assert "Sonic" in titles and "Nomatch" not in titles
