"""Save-file detection (READ-ONLY) — match emulator saves to ROMs by filename stem,
flag which games have a save, and NEVER edit or delete a save."""

import json
from pathlib import Path

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import settings as app_settings
from app.services.saves import scan_saves, _classify, _save_stem, _rom_stem


def _entry(file_name, system="NES"):
    with Session(engine) as s:
        e = LibraryEntry(game_title=Path(file_name).stem, system=system,
                         file_name=file_name, file_path=f"/roms/{file_name}")
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def test_classify_save_kinds():
    assert _classify(Path("Game.srm")) == "battery"
    assert _classify(Path("Game.sav")) == "battery"
    assert _classify(Path("Game.mcr")) == "battery"
    assert _classify(Path("Game.state")) == "state"
    assert _classify(Path("Game.state2")) == "state"
    assert _classify(Path("Game.st0")) == "state"
    assert _classify(Path("Game.nes")) is None        # a ROM, not a save
    assert _classify(Path("Game.zip")) is None


def test_stem_matching():
    assert _save_stem(Path("Mario (USA).srm")) == "mario (usa)"
    assert _save_stem(Path("Mario (USA).nes.srm")) == "mario (usa)"   # emulator kept ROM ext
    assert _save_stem(Path("Mario (USA).state2")) == "mario (usa)"
    assert _rom_stem("Mario (USA).nes") == "mario (usa)"
    assert _rom_stem("Mario (USA).nes.zip") == "mario (usa)"          # archive + inner ext


def test_scan_matches_saves_by_stem(client, tmp_path):
    saves = tmp_path / "saves"; saves.mkdir()
    (saves / "Sonic (USA).srm").write_bytes(b"x")
    (saves / "Sonic (USA).state1").write_bytes(b"x")
    with Session(engine) as s:
        app_settings.set(s, "saves_dir", str(saves)); s.commit()
    eid = _entry("Sonic (USA).md", system="Sega Genesis / Mega Drive")
    with Session(engine) as s:
        scan_saves(s)
        e = s.get(LibraryEntry, eid)
        assert e.save_count == 2
        kinds = {x["kind"] for x in json.loads(e.save_files)}
        assert kinds == {"battery", "state"}
    assert (saves / "Sonic (USA).srm").exists()        # scan never deletes a save


def test_scan_no_match_leaves_zero(client, tmp_path):
    saves = tmp_path / "saves"; saves.mkdir()
    (saves / "Some Other Game.srm").write_bytes(b"x")
    with Session(engine) as s:
        app_settings.set(s, "saves_dir", str(saves)); s.commit()
    eid = _entry("Sonic (USA).md", system="Sega Genesis / Mega Drive")
    with Session(engine) as s:
        scan_saves(s)
        assert s.get(LibraryEntry, eid).save_count == 0


def test_delete_file_never_touches_a_save(client, tmp_path):
    # CRITICAL: deleting a ROM file must leave a save sitting beside it intact.
    rom = tmp_path / "Game.nes"; rom.write_bytes(b"x")
    save = tmp_path / "Game.srm"; save.write_bytes(b"s")
    with Session(engine) as s:
        e = LibraryEntry(game_title="Game", system="NES", file_name="Game.nes", file_path=str(rom))
        s.add(e); s.commit(); s.refresh(e); eid = e.id
    client.post(f"/collection/library/{eid}/delete?delete_file=true")
    assert not rom.exists()        # ROM deleted
    assert save.exists()           # save preserved — the app never deletes saves
