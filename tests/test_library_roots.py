"""Multiple ROM directories — per-directory console mapping, primary seeding +
root_id backfill, multi-root scan, cross-directory duplicate detection, and moving."""

import json
import os

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry, LibraryRoot
from app.services import settings as app_settings
from app.services import library_roots


# --- Pure helpers (no DB) --------------------------------------------------

def test_resolve_system_and_dest_folder_roundtrip():
    # folder→system map (the per-directory direction)
    root = LibraryRoot(path="/x", folder_map=json.dumps({"psx": "PlayStation", "snezz": "SNES"}))
    assert library_roots.resolve_system(root, "psx") == "PlayStation"
    assert library_roots.resolve_system(root, "snezz") == "SNES"
    # falls back to the built-in default aliases, then the folder name itself
    assert library_roots.resolve_system(root, "Sega Genesis") == "Sega Genesis / Mega Drive"
    assert library_roots.resolve_system(root, "totally-unknown") == "totally-unknown"
    # filing inverts the map (first folder that maps to the system)
    assert library_roots.dest_folder_for_system(root, "PlayStation") == "psx"
    # unmapped system → the global default folder name
    assert library_roots.dest_folder_for_system(root, "NES") == "Nintendo Entertainment System"


def test_root_for_path_longest_prefix():
    roots = [
        LibraryRoot(id=1, path=os.path.normpath("/mnt/roms")),
        LibraryRoot(id=2, path=os.path.normpath("/mnt/roms/extra")),  # nested — longer prefix wins
        LibraryRoot(id=3, path=os.path.normpath("/mnt/other")),
    ]
    assert library_roots.root_for_path(roots, os.path.normpath("/mnt/roms/NES/g.nes")).id == 1
    assert library_roots.root_for_path(roots, os.path.normpath("/mnt/roms/extra/SNES/g.sfc")).id == 2
    assert library_roots.root_for_path(roots, os.path.normpath("/mnt/other/g.iso")).id == 3
    assert library_roots.root_for_path(roots, os.path.normpath("/nowhere/g.nes")) is None


# --- Seeding + backfill ----------------------------------------------------

def test_ensure_primary_seeds_and_backfills(fresh_engine, tmp_path):
    with Session(fresh_engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        app_settings.set(s, "folder_map", json.dumps({"PlayStation": "psx"}))  # legacy system→folder
        s.add(LibraryEntry(game_title="G", system="PlayStation",
                           file_name="g.cue", file_path=str(tmp_path / "psx" / "g.cue")))
        s.commit()

    with Session(fresh_engine) as s:
        library_roots.ensure_primary_and_backfill(s)

    with Session(fresh_engine) as s:
        roots = library_roots.get_roots(s)
        assert len(roots) == 1
        assert roots[0].is_primary
        assert os.path.normpath(roots[0].path) == os.path.normpath(str(tmp_path))
        # legacy {system: folder} migrated to the per-root {folder: system} direction
        assert json.loads(roots[0].folder_map) == {"psx": "PlayStation"}
        e = s.exec(select(LibraryEntry)).first()
        assert e.root_id == roots[0].id   # backfilled by longest-prefix match


# --- Endpoint-level (full app via the client fixture) ----------------------

def _reset_roots(dirs):
    """dirs: list of (path, label, is_primary, folder_map_dict). Replaces all roots."""
    with Session(engine) as s:
        for r in s.exec(select(LibraryRoot)).all():
            s.delete(r)
        s.commit()
        for i, (path, label, primary, fmap) in enumerate(dirs):
            s.add(LibraryRoot(path=str(path), label=label, is_primary=primary,
                              position=i, folder_map=json.dumps(fmap)))
        s.commit()
        return {r.label: r.id for r in s.exec(select(LibraryRoot)).all()}


def test_multiroot_scan_assigns_root_id_and_maps_console(client, tmp_path):
    a = tmp_path / "A"; (a / "NES").mkdir(parents=True)
    (a / "NES" / "Game A (USA).nes").write_bytes(b"x")
    b = tmp_path / "B"; (b / "my-snes").mkdir(parents=True)
    (b / "my-snes" / "Game B (USA).sfc").write_bytes(b"y")

    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(a))   # primary path stays synced to A
    ids = _reset_roots([(a, "A", True, {}), (b, "B", False, {"my-snes": "SNES"})])

    r = client.post("/collection/bulk/scan")
    assert r.status_code == 200

    with Session(engine) as s:
        rows = {e.game_title: e for e in s.exec(select(LibraryEntry)).all()}
    ga = next(e for t, e in rows.items() if "Game A" in t)
    gb = next(e for t, e in rows.items() if "Game B" in t)
    assert ga.root_id == ids["A"] and ga.system == "NES"
    # per-directory mapping resolved the oddly-named folder to a console
    assert gb.root_id == ids["B"] and gb.system == "SNES"


def test_cross_directory_duplicate_flagged(client, tmp_path):
    from app.routers.collection import _build_collection
    from app.services.duplicates import recompute_duplicates
    a = tmp_path / "A"; b = tmp_path / "B"; a.mkdir(); b.mkdir()
    ids = _reset_roots([(a, "A", True, {}), (b, "B", False, {})])

    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Dup", system="NES", file_name="d.nes",
                           file_path=str(a / "d.nes"), file_hash="HASH", root_id=ids["A"]))
        s.add(LibraryEntry(game_title="Dup", system="NES", file_name="d.nes",
                           file_path=str(b / "d.nes"), file_hash="HASH", root_id=ids["B"]))
        s.commit()
        recompute_duplicates(s)

    with Session(engine) as s:
        items = _build_collection(s)
    dup_items = [i for i in items if i["game_title"] == "Dup"]
    assert dup_items and all(i["cross_dir_dup"] for i in dup_items)


def test_move_relocates_file_and_updates_root(client, tmp_path):
    a = tmp_path / "A"; (a / "NES").mkdir(parents=True)
    f = a / "NES" / "Game (USA).nes"; f.write_bytes(b"x")
    b = tmp_path / "B"; b.mkdir()

    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(a))
    ids = _reset_roots([(a, "A", True, {}), (b, "B", False, {})])
    with Session(engine) as s:
        e = LibraryEntry(game_title="Game", system="NES", file_name="Game (USA).nes",
                         file_path=str(f), root_id=ids["A"])
        s.add(e); s.commit(); eid = e.id

    r = client.post(f"/collection/library/{eid}/move", data={"dest_root_id": ids["B"]})
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") == "true"

    with Session(engine) as s:
        e = s.get(LibraryEntry, eid)
    assert e.root_id == ids["B"]
    assert not f.exists()                                   # moved off the source
    # filed into B under the default NES folder name
    assert (b / "Nintendo Entertainment System" / "Game (USA).nes").exists()
    assert os.path.normpath(str(b)) in os.path.normpath(e.file_path)


def test_move_refused_into_readonly_directory(client, tmp_path):
    a = tmp_path / "A"; (a / "NES").mkdir(parents=True)
    f = a / "NES" / "Game (USA).nes"; f.write_bytes(b"x")
    b = tmp_path / "B"; b.mkdir()

    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(a))
    ids = _reset_roots([(a, "A", True, {}), (b, "B", False, {})])
    with Session(engine) as s:
        rb = s.get(LibraryRoot, ids["B"]); rb.readonly = True; s.add(rb)
        e = LibraryEntry(game_title="Game", system="NES", file_name="Game (USA).nes",
                         file_path=str(f), root_id=ids["A"])
        s.add(e); s.commit(); eid = e.id

    r = client.post(f"/collection/library/{eid}/move", data={"dest_root_id": ids["B"]})
    assert r.status_code == 200
    assert "read-only" in r.text.lower()
    with Session(engine) as s:
        e = s.get(LibraryEntry, eid)
    assert e.root_id == ids["A"]    # unchanged
    assert f.exists()               # not moved
