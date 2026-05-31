"""Award-tier derivation (services/mastery.py) + the collection Mastered filter."""
from sqlmodel import Session, select

from app.db.models import LibraryEntry, RAGameProgress
from app.services.mastery import sync_library_awards


def test_awards_from_mirror(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(RAGameProgress(game_id=100, title="Super Mario", highest_award_kind="mastered"))
        s.add(RAGameProgress(game_id=200, title="Sonic", highest_award_kind="beaten"))
        s.add(RAGameProgress(game_id=300, title="Zelda", highest_award_kind="completed"))
        s.add(LibraryEntry(game_title="Super Mario", system="NES", file_name="m.nes", file_path="/m.nes", ra_game_id=100))
        s.add(LibraryEntry(game_title="Sonic", system="Genesis", file_name="s.md", file_path="/s.md", ra_game_id=200))
        s.add(LibraryEntry(game_title="Zelda", system="NES", file_name="z.nes", file_path="/z.nes", ra_game_id=300))
        s.add(LibraryEntry(game_title="Orphan", system="NES", file_name="o.nes", file_path="/o.nes", ra_game_id=999))
        s.commit()

        res = sync_library_awards(s)
        assert res["mastered"] == 1
        assert res["beaten"] == 2  # "beaten" + "completed" both count toward the beaten tally

        awards = {e.game_title: e.ra_award for e in s.exec(select(LibraryEntry)).all()}
        assert awards["Super Mario"] == "mastered"
        assert awards["Sonic"] == "beaten"
        assert awards["Zelda"] == "completed"   # softcore 100% — NOT mastered
        assert awards["Orphan"] == ""           # no mirror row


def test_softcore_completed_is_not_mastered(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(RAGameProgress(game_id=300, title="Zelda", highest_award_kind="completed"))
        s.add(LibraryEntry(game_title="Zelda", system="NES", file_name="z.nes", file_path="/z.nes", ra_game_id=300))
        s.commit()
        res = sync_library_awards(s)
        assert res["mastered"] == 0
        e = s.exec(select(LibraryEntry)).first()
        assert e.ra_award == "completed"


def test_subset_entry_resolves_by_title(fresh_engine):
    """A subset copy usually shares the base game's hash, so its ra_game_id points at the
    base. Its award must reflect the subset's own progress (resolved by title), not the base."""
    with Session(fresh_engine) as s:
        s.add(RAGameProgress(game_id=100, title="Super Mario", highest_award_kind="mastered"))
        s.add(RAGameProgress(game_id=101, title="Super Mario [Subset - Bonus]", highest_award_kind="beaten"))
        s.add(LibraryEntry(game_title="Super Mario [Subset - Bonus]", system="NES",
                           file_name="m.nes", file_path="/sub.nes", ra_game_id=100))
        s.commit()
        sync_library_awards(s)
        e = s.exec(select(LibraryEntry)).first()
        assert e.ra_award == "beaten"


def test_collection_mastered_filter(client):
    from app.db.database import engine
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Mastered Game", system="NES", file_name="m.nes",
                           file_path="/m.nes", file_hash="h1", ra_award="mastered"))
        s.add(LibraryEntry(game_title="Plain Game", system="NES", file_name="p.nes",
                           file_path="/p.nes", file_hash="h2", ra_award=""))
        s.commit()

    r = client.get("/collection?status=has_mastered")
    assert r.status_code == 200
    assert "Mastered Game" in r.text
    assert "Plain Game" not in r.text
