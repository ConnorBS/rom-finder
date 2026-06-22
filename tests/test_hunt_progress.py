"""Auto-hunt now creates a live Download row per attempt (progress card + Cancel):
- on verify it REUSES that one row (promoted to pending_approval), never a 2nd row;
- on bad_hash/failure it DELETES the transient row (the HuntAttempt is the record).

Drives the real `hunter.auto_hunt` with a fake source + fake RAClient (no network).
"""
import asyncio

import pytest
from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import (
    AppSetting, Download, DownloadStatus, HuntAttempt, HuntStatus, WantedGame,
)
from app.services import hunter


class _FakeSource:
    source_id = "fakesrc"
    name = "Fake"
    available = True

    def __init__(self, fname="Test Game (USA).nes", content=b"romdata"):
        self._fname = fname
        self._content = content
        self.progress_seen = []

    async def search(self, query, system=""):
        return [{"identifier": "coll1", "title": "Test Game", "source_id": self.source_id}]

    async def get_files(self, identifier, name_filter=""):
        return [{"name": self._fname, "identifier": identifier,
                 "source_id": self.source_id, "size": len(self._content)}]

    def get_download_url(self, identifier, filename):
        return f"http://fake/{identifier}/{filename}"

    async def download_file(self, url, dest, progress_callback=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if progress_callback:
            await progress_callback(0.5)
            self.progress_seen.append(0.5)
        dest.write_bytes(self._content)
        if progress_callback:
            await progress_callback(1.0)


class _FakeRA:
    """get_game_hashes_full returns the accepted MD5 'aacc11'; lookup_hash is
    controlled by the test via `match_id`."""
    match_id = None

    def __init__(self, *a, **k):
        pass

    async def get_game_hashes_full(self, gid):
        return [{"MD5": "aacc11", "Name": "Test Game (USA).nes"}]

    async def lookup_hash(self, h):
        return {"ID": _FakeRA.match_id} if _FakeRA.match_id else None


def _seed(tmp_path, ra_game_id=555):
    roms = tmp_path / "roms"
    check = tmp_path / "check"
    roms.mkdir(); check.mkdir()
    with Session(engine) as s:
        for k, v in {
            "ra_username": "u", "ra_api_key": "k",
            "download_dir": str(roms), "check_dir": str(check),
            "use_review_dir": "true",
        }.items():
            s.add(AppSetting(key=k, value=v))
        g = WantedGame(game_title="Test Game", system="NES", ra_game_id=ra_game_id,
                       status=HuntStatus.hunting)
        s.add(g)
        s.commit()
        s.refresh(g)
        return g.id


def _patch(monkeypatch, file_hash):
    src = _FakeSource()
    monkeypatch.setattr(hunter, "_enabled_srcs", lambda session: [src])
    monkeypatch.setattr(hunter, "RAClient", _FakeRA)

    async def _fake_hash(path, system):
        return file_hash, True
    monkeypatch.setattr(hunter, "ra_hash_or_fallback", _fake_hash)
    return src


def test_verified_hunt_reuses_one_download_row(fresh_engine, tmp_path, monkeypatch):
    wid = _seed(tmp_path)
    _FakeRA.match_id = None  # verification rides the accepted-hash list, not lookup
    src = _patch(monkeypatch, file_hash="aacc11")  # in get_game_hashes_full → verified

    asyncio.run(hunter.auto_hunt(wid))

    with Session(engine) as s:
        dls = s.exec(select(Download)).all()
        assert len(dls) == 1, "verify must reuse the transient row, not create a 2nd"
        d = dls[0]
        assert d.status == DownloadStatus.pending_approval
        assert d.progress == 1.0 and d.hash_verified is True and d.file_path
        assert d.hunt_task_id is None  # terminal — no longer a cancellable in-flight row
        assert src.progress_seen == [0.5]  # the real progress callback fired
        attempts = s.exec(select(HuntAttempt)).all()
        assert [a.result for a in attempts] == ["verified"]
        assert s.get(WantedGame, wid).status == HuntStatus.verified


def test_bad_hash_hunt_leaves_no_download_row(fresh_engine, tmp_path, monkeypatch):
    wid = _seed(tmp_path)
    _FakeRA.match_id = 999  # hash resolves to a DIFFERENT game → wrong dump
    _patch(monkeypatch, file_hash="deadbeef")  # NOT in the accepted list

    asyncio.run(hunter.auto_hunt(wid))

    with Session(engine) as s:
        assert s.exec(select(Download)).all() == [], "transient row must be deleted on bad_hash"
        attempts = s.exec(select(HuntAttempt)).all()
        assert [a.result for a in attempts] == ["bad_hash"]
        assert s.get(WantedGame, wid).status == HuntStatus.exhausted


def test_max_candidates_cap_raised():
    # Flood-guard kept, but raised so an exhaustive multi-source/region candidate
    # list isn't truncated before a verified dump is reached.
    assert hunter._MAX_CANDIDATES >= 40


def test_cancel_between_candidates_skips_external(fresh_engine, tmp_path, monkeypatch):
    """A cancelled hunt must NOT fall through to the torrent/usenet last-resort
    fallback — that was what left the game 'awaiting_external', polled forever with
    no way to cancel from the queue (the reported bug)."""
    from app.services import activity as activity_store
    wid = _seed(tmp_path)
    _FakeRA.match_id = None
    _patch(monkeypatch, file_hash="deadbeef")

    called = {"external": False}

    async def _spy_submit(*a, **k):
        called["external"] = True
        return False
    monkeypatch.setattr("app.services.external_hunt.submit_external", _spy_submit)

    # User cancels before the candidate loop even starts.
    activity_store.cancel(f"hunt-{wid}")
    asyncio.run(hunter.auto_hunt(wid))

    assert called["external"] is False, "cancel must not trigger the external fallback"
    with Session(engine) as s:
        assert s.exec(select(Download)).all() == [], "no orphaned transient download row"
        assert s.get(WantedGame, wid).status == HuntStatus.exhausted
    # finish() must clear the cancel flag so a later re-hunt isn't auto-cancelled.
    assert activity_store.is_cancelled(f"hunt-{wid}") is False


def test_cancel_mid_download_aborts_attempt(fresh_engine, tmp_path, monkeypatch):
    """Cancelling while a download is in flight aborts that attempt promptly (not
    after the 5-min timeout), deletes its transient card, records NO HuntAttempt,
    and stops the hunt without the external fallback."""
    from app.services import activity as activity_store
    wid = _seed(tmp_path)
    _FakeRA.match_id = None
    monkeypatch.setattr(hunter, "_CANCEL_POLL", 0.01)

    task_id = f"hunt-{wid}"

    class _SlowSource(_FakeSource):
        async def download_file(self, url, dest, progress_callback=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            activity_store.cancel(task_id)   # user hits Cancel mid-download
            await asyncio.sleep(5)           # the cancel poll aborts this first
            dest.write_bytes(self._content)

    src = _SlowSource()
    monkeypatch.setattr(hunter, "_enabled_srcs", lambda session: [src])
    monkeypatch.setattr(hunter, "RAClient", _FakeRA)

    async def _fake_hash(path, system):
        return "deadbeef", True
    monkeypatch.setattr(hunter, "ra_hash_or_fallback", _fake_hash)

    called = {"external": False}

    async def _spy_submit(*a, **k):
        called["external"] = True
        return False
    monkeypatch.setattr("app.services.external_hunt.submit_external", _spy_submit)

    asyncio.run(hunter.auto_hunt(wid))

    assert called["external"] is False
    with Session(engine) as s:
        assert s.exec(select(Download)).all() == [], "cancelled attempt leaves no card"
        assert s.exec(select(HuntAttempt)).all() == [], "a cancelled attempt is not a real try"
        assert s.get(WantedGame, wid).status == HuntStatus.exhausted
