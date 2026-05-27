"""Disc rips are one game (the .cue); their .bin/.img tracks must not be imported
as separate ROMs — and tracks imported before this rule get cleaned up on scan.
Common when a disc is unzipped (e.g. for an Android handheld)."""

from pathlib import Path

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry
from app.routers.library import is_disc_track
from app.services import settings as app_settings


def test_is_disc_track(tmp_path):
    (tmp_path / "Game.cue").write_text("FILE \"Game (Track 01).bin\" BINARY")
    track1 = tmp_path / "Game (Track 01).bin"; track1.write_bytes(b"data")
    track2 = tmp_path / "Game (Track 02).bin"; track2.write_bytes(b"audio")
    standalone = tmp_path / "solo"; standalone.mkdir()
    lone_bin = standalone / "raw.bin"; lone_bin.write_bytes(b"x")
    cart = tmp_path / "game.nes"; cart.write_bytes(b"NES\x1a")

    assert is_disc_track(track1)          # .bin next to a .cue → track
    assert is_disc_track(track2)          # audio track too
    assert not is_disc_track(lone_bin)    # .bin with no .cue sibling → standalone
    assert not is_disc_track(cart)        # not a .bin/.img at all


def test_disc_track_cache_reused(tmp_path):
    (tmp_path / "d.cue").write_text("x")
    b = tmp_path / "d (Track 01).bin"; b.write_bytes(b"x")
    cache: dict = {}
    assert is_disc_track(b, cache)
    assert str(tmp_path) in cache         # folder result memoised


def _make_disc(base: Path):
    ps = base / "PlayStation"; ps.mkdir(parents=True)
    (ps / "Firebugs.cue").write_text("FILE \"Firebugs (Track 01).bin\" BINARY")
    for n in (1, 2, 3):
        (ps / f"Firebugs (Track 0{n}).bin").write_bytes(b"x" * 10)
    return ps


def test_scan_imports_cue_not_tracks_and_cleans_existing(client, tmp_path):
    ps = _make_disc(tmp_path)
    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(tmp_path))
        # An audio track imported by an older scan, sitting as no_ra.
        s.add(LibraryEntry(game_title="Firebugs (Track 02)", system="PlayStation",
                           file_name="Firebugs (Track 02).bin",
                           file_path=str(ps / "Firebugs (Track 02).bin"),
                           file_hash="stale", ra_matched=False))
        s.commit()

    r = client.post("/collection/bulk/scan")
    assert r.status_code == 200
    assert "disc-track artifacts removed" in r.text

    with Session(engine) as s:
        entries = s.exec(select(LibraryEntry)).all()
        names = {e.file_name for e in entries}
    assert "Firebugs.cue" in names                     # the disc imported as one entry
    assert not any(n.endswith(".bin") for n in names)  # no track .bin entries remain
