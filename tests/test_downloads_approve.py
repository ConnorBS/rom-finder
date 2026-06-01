"""Approve / Approve-all move the file to the ROMs dir via a background task
(non-blocking, with an activity-tray entry) and leave a LibraryEntry behind.

The blocking move was previously done inline in the request, which froze the page
with no feedback — now it runs in the background and the card shows a 'Moving…'
state until the row is gone."""

from pathlib import Path

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import Download, DownloadStatus, LibraryEntry
from app.routers.downloads import _resolve_folder
from app.services import settings as app_settings


def _point_dirs_at(tmp_path: Path):
    roms = tmp_path / "roms"
    review = tmp_path / "rom-check"
    roms.mkdir()
    review.mkdir()
    with Session(engine) as s:
        app_settings.set(s, "download_dir", str(roms))
        app_settings.set(s, "check_dir", str(review))
        app_settings.set(s, "download_dir_readonly", "false")
        s.commit()
    return roms, review


def _make_pending(review: Path, title: str, system: str, name: str) -> int:
    folder = review / system
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / name
    src.write_bytes(b"rom-bytes-" + name.encode())
    with Session(engine) as s:
        d = Download(
            game_title=title, system=system, file_name=name, file_path=str(src),
            source_url="http://x/y", source_id="archive_org", archive_identifier="x",
            status=DownloadStatus.pending_approval, file_hash="abc123", hash_verified=True,
            ra_game_id=42,
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def test_approve_moves_file_and_creates_library_entry(client, tmp_path):
    roms, review = _point_dirs_at(tmp_path)
    did = _make_pending(review, "Mort the Chicken", "PlayStation", "Mort the Chicken (USA).bin")

    r = client.post(f"/downloads/{did}/approve")
    assert r.status_code == 200
    assert "Moving to ROMs directory" in r.text  # card flips to a Moving… state immediately

    # TestClient runs the BackgroundTask after the response, so the move is done now.
    dest = roms / _resolve_folder({}, "PlayStation") / "Mort the Chicken (USA).bin"
    assert dest.exists(), "file should have moved into the ROMs dir"
    assert not (review / "PlayStation" / "Mort the Chicken (USA).bin").exists()

    with Session(engine) as s:
        assert s.get(Download, did) is None  # row removed after a successful move
        entry = s.exec(select(LibraryEntry).where(LibraryEntry.ra_game_id == 42)).first()
        assert entry is not None and entry.file_path == str(dest)
        assert entry.hash_verified is True


def test_approve_all_moves_every_pending_item(client, tmp_path):
    roms, review = _point_dirs_at(tmp_path)
    ids = [
        _make_pending(review, "Game A", "NES", "Game A (USA).nes"),
        _make_pending(review, "Game B", "SNES", "Game B (USA).sfc"),
    ]

    r = client.post("/downloads/approve-all")
    assert r.status_code == 200

    for system, name in (("NES", "Game A (USA).nes"), ("SNES", "Game B (USA).sfc")):
        assert (roms / _resolve_folder({}, system) / name).exists()

    with Session(engine) as s:
        for did in ids:
            assert s.get(Download, did) is None
        assert len(s.exec(select(LibraryEntry)).all()) == 2


def test_approve_blocked_when_roms_dir_readonly(client, tmp_path):
    roms, review = _point_dirs_at(tmp_path)
    with Session(engine) as s:
        app_settings.set(s, "download_dir_readonly", "true")
        s.commit()
    did = _make_pending(review, "Locked Game", "NES", "Locked (USA).nes")

    r = client.post(f"/downloads/{did}/approve")
    assert "read-only" in r.text.lower()
    # Nothing moved; the row is untouched.
    with Session(engine) as s:
        d = s.get(Download, did)
        assert d is not None and d.status == DownloadStatus.pending_approval
    assert (review / "NES" / "Locked (USA).nes").exists()
