"""external_hunt.poll_active state machine: a completed torrent job is ingested,
hash-verified, and its linked Download promoted to pending_approval (Wanted →
verified); a bad-hash job fails (Wanted → exhausted, transient Download deleted).

Uses a fake DownloadClient + a real staged file; RA hash/lookup are monkeypatched.
"""
import asyncio
import json

import pytest
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import (
    AppSetting, Download, DownloadStatus, ExternalDownload, HuntAttempt,
    HuntStatus, WantedGame,
)
from app.services import external_hunt
from app.services.download_clients import registry as client_registry
from app.services.download_clients.base import DownloadClient


class _FakeClient(DownloadClient):
    client_id = "download_client"
    name = "Fake"
    protocols = {"torrent"}

    def __init__(self, content_dir):
        self._dir = content_dir
        self.completed = True
        self.progress = 1.0
        self.files = []          # torrents/files response for the selection path
        self.set_calls = []      # recorded set_wanted_files(keep_indices) calls

    async def search(self, query, system=""):
        return []

    async def submit(self, release, save_path):
        return {"job_handle": "h1", "protocol": "torrent", "needs_file_selection": False}

    async def status(self, job_handle, protocol):
        return {"state": "uploading" if self.completed else "downloading",
                "progress": self.progress, "completed": self.completed, "failed": False,
                "content_path": str(self._dir), "save_path": str(self._dir), "error": ""}

    async def list_files(self, job_handle):
        return self.files

    async def set_wanted_files(self, job_handle, keep_indices):
        self.set_calls.append(list(keep_indices))

    async def cleanup(self, job_handle, protocol, delete_files=False):
        self.cleaned = (job_handle, delete_files)


@pytest.fixture
def fake_client(tmp_path):
    content = tmp_path / "qbit" / "Game"
    content.mkdir(parents=True)
    (content / "Test Game (USA).nes").write_bytes(b"rom")
    client = _FakeClient(content)
    client_registry.register(client)
    yield client, tmp_path
    client_registry.unregister("download_client")


def _seed(tmp_path, accepted, ra_game_id=42, needs_sel=False):
    check = tmp_path / "check"; check.mkdir()
    with Session(engine) as s:
        for k, v in {"use_review_dir": "true", "check_dir": str(check),
                     "download_dir": str(tmp_path / "roms"),
                     "ra_username": "u", "ra_api_key": "k",
                     "download_client_download_client_enabled": "true"}.items():
            s.add(AppSetting(key=k, value=v))
        g = WantedGame(game_title="Test Game", system="NES", ra_game_id=ra_game_id,
                       status=HuntStatus.awaiting_external)
        s.add(g); s.commit(); s.refresh(g)
        dl = Download(game_title="Test Game", system="NES", file_name="rel.torrent",
                      file_path=None, source_url="", source_id="download_client",
                      status=DownloadStatus.downloading, progress=0.5, hunt_task_id=f"hunt-{g.id}")
        s.add(dl); s.commit(); s.refresh(dl)
        ext = ExternalDownload(
            wanted_game_id=g.id, download_id=dl.id, client_id="download_client",
            protocol="torrent", job_handle="h1", release_title="Test Game (USA)",
            status="downloading", needs_file_selection=needs_sel,
            match_data=json.dumps({"ra_stems": ["test game (usa)"], "title_terms": ["test", "game"],
                                   "accepted_md5s": list(accepted)}),
        )
        s.add(ext); s.commit(); s.refresh(ext)
        return g.id, dl.id, ext.id


def _patch_ra(monkeypatch, file_hash, matched_id):
    async def _hash(path, system):
        return file_hash, True
    monkeypatch.setattr(external_hunt, "ra_hash_or_fallback", _hash)

    class _RA:
        def __init__(self, *a, **k): pass
        async def lookup_hash(self, h):
            return {"ID": matched_id} if matched_id else None
    monkeypatch.setattr(external_hunt, "RAClient", _RA)


def test_completed_job_ingests_and_verifies(fresh_engine, fake_client, monkeypatch):
    client, tmp_path = fake_client
    wid, dlid, _ = _seed(tmp_path, accepted={"aacc11"})
    _patch_ra(monkeypatch, file_hash="aacc11", matched_id=None)  # verifies via accepted list

    counts = asyncio.run(external_hunt.poll_active())
    assert counts["completed"] == 1

    with Session(engine) as s:
        assert s.get(WantedGame, wid).status == HuntStatus.verified
        d = s.get(Download, dlid)
        assert d.status == DownloadStatus.pending_approval and d.hash_verified and d.file_path
        assert d.hunt_task_id is None
        ext = s.exec(select(ExternalDownload)).first()
        assert ext.status == "verified"
        assert [a.result for a in s.exec(select(HuntAttempt)).all()] == ["verified"]
    # staged into the review dir
    assert list((tmp_path / "check").rglob("Test Game (USA).nes"))


def test_bad_hash_job_fails_and_re_exhausts(fresh_engine, fake_client, monkeypatch):
    client, tmp_path = fake_client
    wid, dlid, _ = _seed(tmp_path, accepted={"aacc11"})
    _patch_ra(monkeypatch, file_hash="deadbeef", matched_id=999)  # wrong game, not in accepted

    counts = asyncio.run(external_hunt.poll_active())
    assert counts["failed"] == 1

    with Session(engine) as s:
        assert s.get(WantedGame, wid).status == HuntStatus.exhausted
        assert s.get(Download, dlid) is None              # transient row deleted
        ext = s.exec(select(ExternalDownload)).first()
        assert ext.status == "failed"
    assert getattr(client, "cleaned", (None, None))[1] is True   # deleteFiles on bad hash


def test_stalled_job_fails_and_re_exhausts(fresh_engine, fake_client, monkeypatch):
    """Regression for the stall-timer blocker: a flat-progress job must eventually
    fail (it doesn't keep refreshing updated_at on every poll)."""
    import datetime as _dt
    client, tmp_path = fake_client
    client.completed = False
    client.progress = 0.0
    wid, dlid, eid = _seed(tmp_path, accepted={"aacc11"})
    with Session(engine) as s:                       # backdate past the stall window
        s.add(AppSetting(key="external_download_stall_minutes", value="120"))
        ext = s.get(ExternalDownload, eid)
        ext.updated_at = _dt.datetime.utcnow() - _dt.timedelta(minutes=200)
        ext.progress = 0.0
        s.add(ext); s.commit()

    counts = asyncio.run(external_hunt.poll_active())
    assert counts["failed"] == 1
    with Session(engine) as s:
        assert s.get(WantedGame, wid).status == HuntStatus.exhausted
        assert s.get(Download, dlid) is None
        assert s.get(ExternalDownload, eid).status == "failed"


def test_pack_file_selection_trims_then_waits(fresh_engine, fake_client, monkeypatch):
    """needs_file_selection torrent: classify a pack, set qBit file-priority to keep
    only the matching file, store target_files, and keep waiting (not yet complete)."""
    client, tmp_path = fake_client
    client.completed = False
    client.progress = 0.3
    client.files = [
        {"index": 0, "name": "Crash Bandicoot (USA).bin", "size": 100, "priority": 1},
        {"index": 1, "name": "Test Game (USA).nes", "size": 100, "priority": 1},
        {"index": 2, "name": "Spyro (USA).bin", "size": 100, "priority": 1},
    ]
    wid, dlid, eid = _seed(tmp_path, accepted={"aacc11"}, needs_sel=True)
    with Session(engine) as s:                        # job is pre-metadata
        ext = s.get(ExternalDownload, eid); ext.status = "submitted"; s.add(ext); s.commit()

    asyncio.run(external_hunt.poll_active())
    assert client.set_calls == [[1]]                  # kept only the matching index
    with Session(engine) as s:
        ext = s.get(ExternalDownload, eid)
        assert json.loads(ext.target_files) == ["Test Game (USA).nes"]
        assert ext.status in ("metadata", "downloading")   # still in flight
        assert s.get(WantedGame, wid).status == HuntStatus.awaiting_external
