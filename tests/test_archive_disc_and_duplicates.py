"""Two fixes that work together:

1. A disc image shipped inside a .zip/.7z must be EXTRACTED before RAHasher — the
   binary can't mount a disc from within an archive, so it used to fall through to a
   plain MD5 that can never match RA's disc hash (the Little Britain .7z / Dracula EU
   .zip cases). RAHasher must receive the extracted .cue, not the archive.
2. `recompute_duplicates` tags redundant library copies (same content, or same
   game/disc) without tagging legitimate subsets (different discs, .bin tracks)."""

import asyncio
import zipfile

from sqlmodel import Session

from app.services import rahasher
from app.db.models import LibraryEntry
from app.services.duplicates import recompute_duplicates


class _FakeProc:
    returncode = 0

    def __init__(self, out=b"0b011a7282be1b5157713ea3f87a6bb7"):
        self._out = out

    async def communicate(self):
        return (self._out, b"")


def _set_check_dir(engine, path):
    from app.services import settings as app_settings
    with Session(engine) as s:
        app_settings.set(s, "check_dir", str(path))
        s.commit()


def _make_disc_zip(tmp_path):
    z = tmp_path / "Game (USA).zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Game (USA).cue", 'FILE "Game (USA).bin" BINARY\n')
        zf.writestr("Game (USA).bin", b"\x00" * 4096)
    return z


# --- wrapper fix -----------------------------------------------------------

def test_extract_disc_prefers_cue(fresh_engine, tmp_path):
    _set_check_dir(fresh_engine, tmp_path)
    z = _make_disc_zip(tmp_path)
    out = asyncio.run(rahasher._extract_disc_from_archive(z, "PlayStation", 12))
    assert out is not None and out.suffix.lower() == ".cue" and out.exists()


def test_zip_disc_extracted_before_rahasher(fresh_engine, tmp_path, monkeypatch):
    _set_check_dir(fresh_engine, tmp_path)
    z = _make_disc_zip(tmp_path)
    monkeypatch.setattr(rahasher, "_rahasher_available", lambda: True)
    cap = {}

    async def fake_exec(*args, **kw):
        cap["args"] = args
        return _FakeProc()

    monkeypatch.setattr(rahasher.asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(rahasher.compute_ra_hash(z, "PlayStation"))
    assert out == "0b011a7282be1b5157713ea3f87a6bb7"
    # RAHasher must have been pointed at the extracted .cue, never the .zip.
    assert any(str(a).lower().endswith(".cue") for a in cap["args"])
    assert str(z) not in cap["args"]


def test_cartridge_zip_not_disc_extracted(fresh_engine, tmp_path, monkeypatch):
    # A cartridge .zip is NOT a disc → the disc-extract path must leave it alone
    # (RAHasher reads cartridge zips natively).
    _set_check_dir(fresh_engine, tmp_path)
    z = tmp_path / "Game (USA).zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Game (USA).nes", b"NES\x1a" + b"\x00" * 1024)
    monkeypatch.setattr(rahasher, "_rahasher_available", lambda: True)
    cap = {}

    async def fake_exec(*args, **kw):
        cap["args"] = args
        return _FakeProc(out=b"a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")

    monkeypatch.setattr(rahasher.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(rahasher.compute_ra_hash(z, "NES"))
    assert str(z) in cap["args"]            # archive passed straight through


# --- duplicate detection ---------------------------------------------------

def _add(session, **kw):
    e = LibraryEntry(game_title=kw["title"], system=kw.get("system", "PlayStation"),
                     file_name=kw["file_name"], file_path=kw["file_name"],
                     file_hash=kw.get("file_hash"), ra_game_id=kw.get("ra_game_id"),
                     ra_matched=kw.get("ra_matched", False), missing=kw.get("missing", False))
    session.add(e)
    session.flush()
    return e


def test_identical_hash_is_duplicate(fresh_engine):
    with Session(fresh_engine) as s:
        a = _add(s, title="Foo", system="SNES", file_name="Foo.sfc", file_hash="HHH", ra_matched=True)
        b = _add(s, title="Foo Repack", system="SNES", file_name="Foo.zip", file_hash="HHH")
        s.commit()
        recompute_duplicates(s)
        s.refresh(a); s.refresh(b)
        # The matched, non-archive copy is canonical; the .zip is the duplicate.
        assert a.duplicate_of is None
        assert b.duplicate_of == a.id


def test_same_game_different_format_is_duplicate(fresh_engine):
    with Session(fresh_engine) as s:
        cue = _add(s, title="Mr. Bean (Europe)", file_name="Mr. Bean (Europe).cue",
                   file_hash="H1", ra_game_id=22609, ra_matched=True)
        chd = _add(s, title="Mr. Bean (Europe)", file_name="Mr. Bean (Europe).chd",
                   file_hash="H2")   # stale plain-MD5, unmatched, same title
        s.commit()
        recompute_duplicates(s)
        s.refresh(cue); s.refresh(chd)
        assert cue.duplicate_of is None
        assert chd.duplicate_of == cue.id


def test_different_discs_not_duplicate(fresh_engine):
    # Same RA game id, but different discs are SUBSETS — never duplicates of each other.
    with Session(fresh_engine) as s:
        d1 = _add(s, title="FF VII (USA) (Disc 1)", file_name="FF VII (USA) (Disc 1).chd",
                  file_hash="A", ra_game_id=111, ra_matched=True)
        d2 = _add(s, title="FF VII (USA) (Disc 2)", file_name="FF VII (USA) (Disc 2).chd",
                  file_hash="B", ra_game_id=111, ra_matched=True)
        s.commit()
        recompute_duplicates(s)
        s.refresh(d1); s.refresh(d2)
        assert d1.duplicate_of is None and d2.duplicate_of is None


def test_same_ra_game_id_different_titles_not_duplicate(fresh_engine):
    # RA files many DIFFERENT romhacks under one game id (hack collections / subsets).
    # They must NOT be tagged as duplicates of each other — regression guard for the
    # SM64-hack false positives (23 distinct hacks sharing ra_game_id 16767).
    with Session(fresh_engine) as s:
        a = _add(s, title="SM64 - Abandoned Arcade (Hack)", system="Nintendo 64",
                 file_name="SM64 - Abandoned Arcade (Hack).zip", file_hash="AAA",
                 ra_game_id=16767, ra_matched=True)
        b = _add(s, title="SM64 - Arcade Fighter 64 (Hack)", system="Nintendo 64",
                 file_name="SM64 - Arcade Fighter 64 (Hack).zip", file_hash="BBB",
                 ra_game_id=16767, ra_matched=True)
        s.commit()
        recompute_duplicates(s)
        s.refresh(a); s.refresh(b)
        assert a.duplicate_of is None and b.duplicate_of is None


def test_bin_track_not_tagged(fresh_engine):
    # A .bin track shares its .cue's hash but is a COMPONENT — never tag it (deleting
    # it would break the .cue).
    with Session(fresh_engine) as s:
        cue = _add(s, title="Disc Game", file_name="Disc Game.cue", file_hash="SAME", ra_matched=True)
        binf = _add(s, title="Disc Game", file_name="Disc Game.bin", file_hash="SAME", ra_matched=True)
        s.commit()
        recompute_duplicates(s)
        s.refresh(cue); s.refresh(binf)
        assert binf.duplicate_of is None      # track excluded from tagging
        assert cue.duplicate_of is None


def test_recompute_clears_resolved_duplicates(fresh_engine):
    with Session(fresh_engine) as s:
        a = _add(s, title="Solo", system="NES", file_name="Solo.nes", file_hash="Z", ra_matched=True)
        a.duplicate_of = 999            # stale flag from a previous run
        s.add(a); s.commit()
        recompute_duplicates(s)
        s.refresh(a)
        assert a.duplicate_of is None   # no sibling → flag cleared
