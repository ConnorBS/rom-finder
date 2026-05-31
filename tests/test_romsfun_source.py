"""Tests for RomsfunSource (extensions/romsfun.py).

Covers the multi-file enumeration logic — a ROMsFun game page can list more
than one ROM (e.g. a USA dump and a Europe dump) and only one may be the
RA-accepted hash, so get_files must surface every file, not just the first.
No network calls; the loaded-by-path module is tested for its pure helpers.
"""
import importlib.util
from pathlib import Path

_RF_PATH = Path(__file__).resolve().parent.parent / "extensions" / "romsfun.py"
_spec = importlib.util.spec_from_file_location("romfinder_ext_romsfun", _RF_PATH)
_rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rf)

_file_paths = _rf._file_paths
_extract_filename = _rf._extract_filename


def test_file_paths_enumerates_multiple_regions():
    base = "freddi-fish-kelp-seed-mystery-12345"
    html = f"""
    <a href="/download/{base}/1">Freddi Fish - Kelp Seed Mystery (USA)</a>
    <a href="/download/{base}/2">Freddi Fish - The Case of the Missing Kelp Seeds (Europe)</a>
    """
    assert _file_paths(html, base) == [
        f"/download/{base}/1",
        f"/download/{base}/2",
    ]


def test_file_paths_normalises_bare_landing_link():
    base = "some-game-99"
    html = f'<a href="/download/{base}">Download</a>'
    # Bare /download/{base} collapses to the default first file /1.
    assert _file_paths(html, base) == [f"/download/{base}/1"]


def test_file_paths_always_includes_first_file():
    base = "only-europe-7"
    # Page only links the second file, but /1 must still be probed.
    html = f'<a href="/download/{base}/2">Europe</a>'
    assert _file_paths(html, base) == [
        f"/download/{base}/1",
        f"/download/{base}/2",
    ]


def test_file_paths_dedups_and_sorts_numerically():
    base = "g-1"
    html = (
        f'<a href="/download/{base}/10">ten</a>'
        f'<a href="/download/{base}/2">two</a>'
        f'<a href="/download/{base}/2">two-again</a>'
    )
    assert _file_paths(html, base) == [
        f"/download/{base}/1",
        f"/download/{base}/2",
        f"/download/{base}/10",
    ]


def test_file_paths_ignores_other_games():
    base = "wanted-game-5"
    html = (
        f'<a href="/download/{base}/1">this game</a>'
        '<a href="/download/some-other-game-9/1">related game</a>'
    )
    assert _file_paths(html, base) == [f"/download/{base}/1"]


def test_extract_filename_from_cdn_url():
    url = "https://cdn.romsfun.com/wii/Freddi%20Fish%20-%20Kelp%20Seed%20Mystery%20(USA).zip?token=abc"
    assert _extract_filename(url) == "Freddi Fish - Kelp Seed Mystery (USA).zip"
