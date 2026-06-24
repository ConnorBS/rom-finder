"""Orphaned hunt-download reaper + Cancel-endpoint fallback.

A hard restart (the SIGKILL on a redeploy) kills a hunt coroutine WITHOUT running
its `finally` cleanup, stranding the attempt's transient `downloading` card — and
the in-memory hunt task is gone too, so the card's Cancel button (which only
signals that task) is a permanent no-op. `reap_orphaned_hunt_downloads` clears
those at startup and as a Cancel-endpoint fallback. External torrent/usenet jobs
(whose card the scheduler poll keeps driving across restarts) are preserved, and
the poller must not resurrect a card the user deleted.
"""
import asyncio
import json

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import (
    Download, DownloadStatus, ExternalDownload, HuntStatus, WantedGame,
)
from app.services import hunter


def _orphan(s, task_id="hunt-1", status=DownloadStatus.downloading):
    d = Download(game_title="G", system="NES", file_name="g.nes", file_path=None,
                 source_url="", source_id="src", status=status, progress=0.0,
                 hunt_task_id=task_id)
    s.add(d)
    s.commit()
    s.refresh(d)
    return d.id


def test_reap_removes_orphan_keeps_external_and_terminal(fresh_engine):
    with Session(engine) as s:
        orphan_id = _orphan(s, "hunt-1")
        # Backed by a live (non-terminal) external job → must be KEPT.
        ext_dl_id = _orphan(s, "hunt-2")
        s.add(ExternalDownload(
            wanted_game_id=1, download_id=ext_dl_id, client_id="qbit",
            protocol="torrent", job_handle="h", status="downloading",
        ))
        # A normal terminal card (no hunt_task_id) → untouched.
        s.add(Download(game_title="T", system="NES", file_name="t.nes",
                       file_path="/x/t.nes", source_url="", source_id="src",
                       status=DownloadStatus.pending_approval))
        s.commit()

    assert hunter.reap_orphaned_hunt_downloads() == 1
    with Session(engine) as s:
        ids = {d.id for d in s.exec(select(Download)).all()}
        assert orphan_id not in ids          # the stranded hunt card is gone
        assert ext_dl_id in ids              # external-backed row preserved
        assert len(ids) == 2                 # external + terminal remain


def test_reap_scoped_to_task_id(fresh_engine):
    with Session(engine) as s:
        _orphan(s, "hunt-1")
        keep = _orphan(s, "hunt-2")
    assert hunter.reap_orphaned_hunt_downloads("hunt-1") == 1
    with Session(engine) as s:
        assert {d.id for d in s.exec(select(Download)).all()} == {keep}


def test_cancel_endpoint_reaps_dead_orphan(client):
    # Startup reaper already ran (no orphans then); insert one as if a restart stranded it.
    with Session(engine) as s:
        oid = _orphan(s, "hunt-77")
    r = client.post("/activity/cancel/hunt-77")
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(Download, oid) is None, "dead-orphan Cancel must clear the card"


def test_cancel_endpoint_keeps_live_hunt_row(client):
    from app.services import activity as activity_store
    activity_store.start("hunt-88", "Hunting: live")   # a live in-memory hunt task
    with Session(engine) as s:
        oid = _orphan(s, "hunt-88")
    try:
        client.post("/activity/cancel/hunt-88")
        # A live hunt cleans up after itself (its `finally`) — the endpoint must NOT
        # reap underneath it (that could delete a row it's about to promote).
        with Session(engine) as s:
            assert s.get(Download, oid) is not None
    finally:
        activity_store.finish("hunt-88")


def test_external_ingest_does_not_resurrect_deleted_card(fresh_engine, tmp_path, monkeypatch):
    """If the user deleted the progress card mid-download, the external poller's
    ingest must NOT recreate it — it treats the job as cancelled and fails it."""
    from app.services import external_hunt
    with Session(engine) as s:
        g = WantedGame(game_title="Ext Game", system="NES", ra_game_id=42,
                       status=HuntStatus.awaiting_external)
        s.add(g)
        s.commit()
        s.refresh(g)
        ext = ExternalDownload(
            wanted_game_id=g.id, download_id=9999,   # points at a now-deleted card
            client_id="noclient", protocol="torrent", job_handle="h",
            status="downloading", match_data=json.dumps({"accepted_md5s": ["abc"]}),
        )
        s.add(ext)
        s.commit()
        s.refresh(ext)
        ext_id = ext.id

    staged = tmp_path / "ext.nes"
    staged.write_bytes(b"rom")
    monkeypatch.setattr(external_hunt, "_staging_dir", lambda session, system: tmp_path)
    monkeypatch.setattr(external_hunt, "_locate_and_stage",
                        lambda content_path, save_path, targets, stage_dir: staged)

    async def _fake_hash(path, system):
        return "abc", True   # in accepted_md5s → would verify, so the guard is what stops it
    monkeypatch.setattr(external_hunt, "ra_hash_or_fallback", _fake_hash)

    res = asyncio.run(external_hunt._ingest(ext_id, {"content_path": str(tmp_path)}))

    assert res == "failed"
    with Session(engine) as s:
        assert s.exec(select(Download)).all() == [], "must not resurrect the deleted card"
        assert s.get(ExternalDownload, ext_id).status == "failed"
    assert not staged.exists(), "the just-staged file is cleaned up on cancel"
