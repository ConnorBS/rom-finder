"""Live-update change signal: /api/changes exposes per-scope fingerprints the
base.html poller diffs to decide when to morph a page in place.

Key invariants:
- every scope key is present and never an error string;
- a new download moves ONLY the downloads scope (scope isolation);
- a progress-only update must NOT move the downloads token (the per-item
  /downloads/{id}/status poll drives progress bars — the page shouldn't churn);
- a structural change (reaching pending_approval) MUST move it;
- a library change with no timestamp column (a cover write) still moves the
  library token, since the fingerprint is an aggregate, not just max(updated_at).
"""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import Download, DownloadStatus, LibraryEntry

SCOPES = ("library", "wanted", "downloads", "hunts", "logs", "scheduler", "dashboard")


def test_changes_shape_and_no_errors(client):
    r = client.get("/api/changes")
    assert r.status_code == 200
    data = r.json()
    for scope in SCOPES:
        assert scope in data, f"missing scope {scope}"
        assert not str(data[scope]).startswith("err:"), f"{scope} errored: {data[scope]}"


def test_new_download_isolates_scope_and_ignores_progress(client):
    base = client.get("/api/changes").json()

    with Session(engine) as s:
        d = Download(
            game_title="Sonic", system="Genesis", file_name="sonic.md",
            source_url="http://example/sonic.md", status=DownloadStatus.downloading,
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        did = d.id

    after_add = client.get("/api/changes").json()
    assert after_add["downloads"] != base["downloads"]   # structural change registered
    assert after_add["library"] == base["library"]       # other scopes untouched

    # Progress-only update must NOT move the downloads token.
    with Session(engine) as s:
        d = s.get(Download, did)
        d.progress = 0.5
        s.add(d)
        s.commit()
    after_progress = client.get("/api/changes").json()
    assert after_progress["downloads"] == after_add["downloads"]

    # Reaching pending_approval (structural) MUST move it.
    with Session(engine) as s:
        d = s.get(Download, did)
        d.status = DownloadStatus.pending_approval
        s.add(d)
        s.commit()
    after_status = client.get("/api/changes").json()
    assert after_status["downloads"] != after_progress["downloads"]


def test_library_token_moves_on_cover_with_no_timestamp(client):
    base = client.get("/api/changes").json()

    with Session(engine) as s:
        e = LibraryEntry(
            game_title="Zelda", system="NES", file_name="z.nes", file_path="/roms/NES/z.nes",
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        eid = e.id

    after_add = client.get("/api/changes").json()
    assert after_add["library"] != base["library"]

    # A cover write touches no timestamp column; the aggregate fingerprint must
    # still move so the collection page picks up the new art.
    with Session(engine) as s:
        e = s.get(LibraryEntry, eid)
        e.cover_path = "covers/1.png"
        s.add(e)
        s.commit()
    after_cover = client.get("/api/changes").json()
    assert after_cover["library"] != after_add["library"]


def test_library_token_counts_each_match_not_just_any(client):
    # Guards against Boolean SUM collapse: going from 1 RA-matched entry to 2 must
    # change the library fingerprint (a bool would read True for both).
    def add_matched(i):
        with Session(engine) as s:
            s.add(LibraryEntry(
                game_title=f"G{i}", system="NES", file_name=f"g{i}.nes",
                file_path=f"/roms/NES/g{i}.nes", file_hash=f"h{i}", ra_matched=True,
            ))
            s.commit()

    add_matched(1)
    one = client.get("/api/changes").json()["library"]
    add_matched(2)
    two = client.get("/api/changes").json()["library"]
    assert one != two
