"""The duplicate-group panel must distinguish truly byte-identical copies (same hash)
from same-title-but-different-dump copies (grouped only by title+system, e.g. Dragon
Quest MSX1 #16399 vs MSX2 #16400) — so a different version is never mistaken for a
redundant copy."""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry


def _pair(canon_hash, sib_hash, canon_ra=None, sib_ra=None):
    with Session(engine) as s:
        canon = LibraryEntry(game_title="Dragon Quest (Japan)", system="MSX",
                             file_name="dq.zip", file_path="/roms/MSX/dq.zip",
                             file_hash=canon_hash, ra_game_id=canon_ra, ra_matched=True)
        s.add(canon)
        s.commit()
        s.refresh(canon)
        sib = LibraryEntry(game_title="Dragon Quest (Japan)", system="MSX",
                           file_name="dq2.zip", file_path="/roms/MSX2/dq.zip",
                           file_hash=sib_hash, ra_game_id=sib_ra, ra_matched=True,
                           duplicate_of=canon.id)
        s.add(sib)
        s.commit()
        return canon.id


def test_different_dump_is_flagged_with_its_ra_game(client):
    cid = _pair("b6a39a89fb0df0be5dff7d64f5fe8900", "a05839977faccd697c4f5c93ccd3c005",
                canon_ra=16400, sib_ra=16399)
    html = client.get(f"/library/{cid}/detail").text
    assert "Duplicate group" in html          # header reflects the mix, not "Same content"
    assert "different dump" in html            # the sibling is tagged as a different dump
    assert "RA #16399" in html                 # and links to ITS distinct RA game
    assert "/game/16399" in html


def test_identical_copies_still_say_same_content(client):
    h = "8e3630186e35d477231bf8fd50e54cdd"
    cid = _pair(h, h, canon_ra=1446, sib_ra=1446)
    html = client.get(f"/library/{cid}/detail").text
    assert "Same content" in html
    assert "identical copy" in html
    assert "different dump" not in html
