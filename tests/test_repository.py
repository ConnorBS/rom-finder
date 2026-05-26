"""Phase 2: minimal repository seam."""

from sqlmodel import Session

from app.db import repository
from app.db.models import Download, DownloadStatus, LibraryEntry, WantedGame, HuntStatus


def test_wanted_by_ra_game_id(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(WantedGame(game_title="Sonic", system="Genesis", ra_game_id=1))
        s.commit()
        assert repository.wanted_by_ra_game_id(s, 1).game_title == "Sonic"
        assert repository.wanted_by_ra_game_id(s, 999) is None


def test_mark_wanted_verified(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(WantedGame(game_title="Mario", system="NES", ra_game_id=7, status=HuntStatus.hunting))
        s.commit()
        w = repository.mark_wanted_verified(s, 7)
        s.commit()
        assert w.status == HuntStatus.verified
        # No-op cases
        assert repository.mark_wanted_verified(s, None) is None
        assert repository.mark_wanted_verified(s, 12345) is None


def test_unverified_library_entries(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(LibraryEntry(game_title="A", system="NES", file_name="a.nes", file_path="/a.nes",
                           file_hash="h1", ra_matched=False))   # unverified → included
        s.add(LibraryEntry(game_title="B", system="NES", file_name="b.nes", file_path="/b.nes",
                           file_hash="h2", ra_matched=True))    # matched → excluded
        s.add(LibraryEntry(game_title="C", system="NES", file_name="c.nes", file_path="/c.nes",
                           file_hash=None, ra_matched=False))   # unhashed → excluded
        s.commit()
        titles = {e.game_title for e in repository.unverified_library_entries(s)}
        assert titles == {"A"}


def test_create_library_entry_from_download(fresh_engine):
    with Session(fresh_engine) as s:
        dl = Download(
            game_title="Zelda", system="NES", file_name="z.nes", file_path="/tmp/z.nes",
            source_url="http://x", file_hash="abc", hash_verified=True, ra_game_id=42,
            status=DownloadStatus.completed,
        )
        s.add(dl); s.commit()
        entry = repository.create_library_entry_from_download(s, dl, "/roms/NES/z.nes")
        s.commit()
        from pathlib import Path
        assert Path(entry.file_path) == Path("/roms/NES/z.nes")  # OS-agnostic
        assert entry.file_name == "z.nes"
        assert entry.file_hash == "abc"        # falls back to download.file_hash
        assert entry.ra_matched is True         # == download.hash_verified
        # file_hash override path
        e2 = repository.create_library_entry_from_download(s, dl, "/roms/NES/z2.nes", file_hash="xyz")
        assert e2.file_hash == "xyz"
