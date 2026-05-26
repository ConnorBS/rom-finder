"""Phase 8: WiiWii root-cause fix (server-side system normalization)."""

from sqlmodel import Session, select

from app.services.title_utils import canonical_system
from app.services.ra_client import SYSTEMS
from app.db.models import WantedGame


def test_exact_double_collapses():
    assert canonical_system("WiiWii", None) == "Wii"


def test_clean_multiword_name_not_mangled():
    # The dangerous case: must NOT collapse SNES to NES via an endswith heuristic.
    assert canonical_system("Super Nintendo Entertainment System", None) == \
        "Super Nintendo Entertainment System"
    assert canonical_system("Nintendo 64", None) == "Nintendo 64"


def test_id_is_authoritative():
    sid, name = next(iter(SYSTEMS.items()))
    assert canonical_system("GarbageGarbage", sid) == name   # id overrides name
    assert canonical_system("", sid) == name


def test_unknown_id_falls_back_to_name_normalization():
    assert canonical_system("WiiWii", 999999) == "Wii"


def test_blank_passthrough():
    assert canonical_system("", None) == ""
    assert canonical_system(None, None) == ""


def test_migration_0010_normalizes_existing_rows(fresh_engine):
    from app.db.migrations import _m_0010_normalize_system_names
    with Session(fresh_engine) as s:
        s.add(WantedGame(game_title="X", system="WiiWii", ra_game_id=111))
        s.commit()
        _m_0010_normalize_system_names(s)
        s.commit()
        row = s.exec(select(WantedGame).where(WantedGame.ra_game_id == 111)).first()
        assert row.system == "Wii"
