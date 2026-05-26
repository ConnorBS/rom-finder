"""Phase 3: schema uniqueness (migrations 0006-0008)."""

import pytest
from sqlmodel import Session
from sqlalchemy.exc import IntegrityError

from app.db.models import WantedGame, LibraryEntry, Download


def test_wanted_ra_system_unique(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(WantedGame(game_title="A", system="NES", ra_game_id=1))
        s.commit()
    with Session(fresh_engine) as s:
        s.add(WantedGame(game_title="A dup", system="NES", ra_game_id=1))
        with pytest.raises(IntegrityError):
            s.commit()


def test_wanted_same_id_different_system_allowed(fresh_engine):
    # Uniqueness is on the (ra_game_id, system) pair, not ra_game_id alone.
    with Session(fresh_engine) as s:
        s.add(WantedGame(game_title="A", system="NES", ra_game_id=1))
        s.add(WantedGame(game_title="A", system="Famicom", ra_game_id=1))
        s.commit()


def test_library_file_path_unique(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(LibraryEntry(game_title="A", system="NES", file_name="a", file_path="/x/a.nes"))
        s.commit()
    with Session(fresh_engine) as s:
        s.add(LibraryEntry(game_title="B", system="NES", file_name="a", file_path="/x/a.nes"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_download_path_unique_but_multiple_nulls_ok(fresh_engine):
    # Partial index: NULL file_path (queued, not yet downloaded) is exempt.
    with Session(fresh_engine) as s:
        s.add(Download(game_title="A", system="NES", file_name="a", source_url="u", file_path=None))
        s.add(Download(game_title="B", system="NES", file_name="b", source_url="u", file_path=None))
        s.commit()
    with Session(fresh_engine) as s:
        s.add(Download(game_title="C", system="NES", file_name="c", source_url="u", file_path="/d/c.nes"))
        s.commit()
    with Session(fresh_engine) as s:
        s.add(Download(game_title="D", system="NES", file_name="d", source_url="u", file_path="/d/c.nes"))
        with pytest.raises(IntegrityError):
            s.commit()
