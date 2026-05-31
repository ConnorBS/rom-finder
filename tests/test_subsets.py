"""Hash-aware subset flags (services/subsets.py::recompute_subset_flags) + the filter."""
import json

from sqlmodel import Session, select

from app.db.models import LibraryEntry, RAGameProgress, RASubsetHash
from app.services.subsets import recompute_subset_flags


def test_subset_flags(fresh_engine):
    with Session(fresh_engine) as s:
        # Subsets 501 (unmastered) + 502 (mastered) both accept hash "abc".
        s.add(RASubsetHash(parent_game_id=100, subset_game_id=501, subset_title="Base [Subset - A]", console_id=7, md5="abc"))
        s.add(RASubsetHash(parent_game_id=100, subset_game_id=502, subset_title="Base [Subset - B]", console_id=7, md5="abc"))
        s.add(RAGameProgress(game_id=502, title="Base [Subset - B]", highest_award_kind="mastered"))
        # Owned base ROM whose hash plays both subsets.
        s.add(LibraryEntry(game_title="Base", system="NES", file_name="b.nes",
                           file_path="/b.nes", file_hash="ABC", ra_game_id=100))
        # A ROM whose hash matches no subset.
        s.add(LibraryEntry(game_title="Other", system="NES", file_name="o.nes",
                           file_path="/o.nes", file_hash="ZZZ", ra_game_id=200))
        # A subset-only copy (title marker).
        s.add(LibraryEntry(game_title="Base (Subset - A)", system="NES", file_name="b.nes",
                           file_path="/sub.nes", file_hash="ABC", ra_game_id=100))
        s.commit()

        res = recompute_subset_flags(s)
        rows = {e.file_path: e for e in s.exec(select(LibraryEntry)).all()}

        base = rows["/b.nes"]
        info = json.loads(base.subset_info)
        assert {i["game_id"] for i in info} == {501, 502}          # matched by hash (case-insensitive)
        assert {i["game_id"] for i in info if not i["mastered"]} == {501}
        assert base.is_subset_rom is False

        assert rows["/o.nes"].subset_info == ""                    # hash matches nothing
        assert rows["/sub.nes"].is_subset_rom is True              # title marker

        assert res["subset_compatible"] == 2                       # /b.nes + /sub.nes
        assert res["subset_available"] == 2                        # both have unfinished subset 501


def test_collection_subset_available_filter(client):
    from app.db.database import engine
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Has Subset", system="NES", file_name="h.nes", file_path="/h.nes",
                           file_hash="h1", subset_info=json.dumps([{"game_id": 9, "title": "X [Subset]", "mastered": False}])))
        s.add(LibraryEntry(game_title="No Subset", system="NES", file_name="n.nes", file_path="/n.nes",
                           file_hash="h2", subset_info=""))
        s.add(LibraryEntry(game_title="Subset Done", system="NES", file_name="d.nes", file_path="/d.nes",
                           file_hash="h3", subset_info=json.dumps([{"game_id": 8, "title": "Y [Subset]", "mastered": True}])))
        s.commit()

    r = client.get("/collection?status=subset_available")
    assert r.status_code == 200
    assert "Has Subset" in r.text
    assert "No Subset" not in r.text
    assert "Subset Done" not in r.text   # compatible, but nothing left to play
