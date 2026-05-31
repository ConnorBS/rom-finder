"""Tests for RomsfunSource (extensions/romsfun.py).

Covers the multi-file enumeration logic — a ROMsFun game page can list more
than one ROM (e.g. a USA dump and a Europe dump) and only one may be the
RA-accepted hash, so get_files must surface every file, not just the first.
No network calls; the loaded-by-path module is tested for its pure helpers.
"""
import asyncio
import importlib.util
from pathlib import Path

_RF_PATH = Path(__file__).resolve().parent.parent / "extensions" / "romsfun.py"
_spec = importlib.util.spec_from_file_location("romfinder_ext_romsfun", _RF_PATH)
_rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rf)

RomsfunSource = _rf.RomsfunSource
_file_paths = _rf._file_paths
_extract_filename = _rf._extract_filename
_normalize_query = _rf._normalize_query
_parse_search_results = _rf._parse_search_results
_parse_cdn_url = _rf._parse_cdn_url


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


# ---------------------------------------------------------------------------
# Query normalization — the apostrophe is what broke ROMsFun's search
# ---------------------------------------------------------------------------

def test_normalize_replaces_apostrophe_with_space():
    # "Dont" (removed) returns 0 results on the site; "Don t" (space) matches.
    assert _normalize_query("Pajama Sam: Don't Fear the Dark") == "Pajama Sam: Don t Fear the Dark"


def test_normalize_handles_ra_stem_hyphen():
    # The hunt's first query is the RA ROM-name stem (uses " - ").
    assert _normalize_query("Pajama Sam - Don't Fear the Dark") == "Pajama Sam - Don t Fear the Dark"


def test_normalize_curly_apostrophe():
    assert _normalize_query("Marko’s Magic Football") == "Marko s Magic Football"


def test_normalize_strips_injected_backslash():
    # ROMsFun's search box injects a backslash before the apostrophe.
    out = _normalize_query("Don\\'t Fear")
    assert "\\" not in out and "'" not in out
    assert out == "Don t Fear"


def test_normalize_collapses_runs_of_whitespace():
    assert _normalize_query("A  \\'  B") == "A B"


def test_normalize_plain_title_unchanged():
    assert _normalize_query("Super Mario Galaxy") == "Super Mario Galaxy"


# ---------------------------------------------------------------------------
# System map + result parsing
# ---------------------------------------------------------------------------

def test_system_map_wii_is_nintendo_wii():
    assert _rf._SYSTEM_MAP["Wii"] == "nintendo-wii"


# Mirrors ROMsFun's catalog card markup: an <a> to the game page inside a
# "bg-white" card, with an <h3> title sibling. Includes a cross-system sibling
# (windows-3x) that the system filter must drop.
_FIXTURE = """
<div class="bg-white card">
  <a href="/roms/nintendo-wii/pajama-sam-dont-fear-the-dark.html"><img alt="cover"></a>
  <h3>Pajama Sam: Don't Fear the Dark</h3>
</div>
<div class="bg-white card">
  <a href="/roms/windows-3x/pajama-sam-no-need-to-hide-when-its-dark-outside-3.html"><img alt="x"></a>
  <h3>Pajama Sam: No Need to Hide When It's Dark Outside</h3>
</div>
"""


def test_parse_keeps_only_expected_system():
    res = _parse_search_results(_FIXTURE, "nintendo-wii", "romsfun")
    assert [r["identifier"] for r in res] == ["nintendo-wii::pajama-sam-dont-fear-the-dark"]


def test_parse_extracts_title_and_source():
    res = _parse_search_results(_FIXTURE, "nintendo-wii", "romsfun")
    assert res[0]["title"] == "Pajama Sam: Don't Fear the Dark"
    assert res[0]["source_id"] == "romsfun"


def test_parse_blank_system_keeps_all():
    res = _parse_search_results(_FIXTURE, "", "romsfun")
    assert len(res) == 2


def test_parse_ignores_non_game_links():
    html = '<a href="/roms/nintendo-wii/">All Wii</a><a href="/blog/post.html">Blog</a>'
    assert _parse_search_results(html, "nintendo-wii", "romsfun") == []


# ---------------------------------------------------------------------------
# search() — per-system endpoint + normalization + system filter, no network
# ---------------------------------------------------------------------------

def test_search_hits_per_system_endpoint_with_normalized_query(monkeypatch):
    captured = {}

    async def fake_fetch(self, url):
        captured["url"] = url
        return _FIXTURE

    monkeypatch.setattr(RomsfunSource, "_fetch_html", fake_fetch)
    results = asyncio.run(RomsfunSource().search("Pajama Sam: Don't Fear the Dark", "Wii"))

    # Per-system catalog endpoint, apostrophe normalized to a space ("Don+t").
    assert "/roms/nintendo-wii/?q=" in captured["url"]
    assert "Don+t" in captured["url"]
    # Only the Wii target is returned (cross-system sibling filtered out).
    assert [r["identifier"] for r in results] == ["nintendo-wii::pajama-sam-dont-fear-the-dark"]


def test_search_unknown_system_falls_back_to_global(monkeypatch):
    captured = {}

    async def fake_fetch(self, url):
        captured["url"] = url
        return ""

    monkeypatch.setattr(RomsfunSource, "_fetch_html", fake_fetch)
    asyncio.run(RomsfunSource().search("Some Game", "NonexistentSystem"))
    assert "/?s=" in captured["url"]


# ---------------------------------------------------------------------------
# Mirror-page CDN URL parsing — replaces the Referer-bound admin-ajax call
# ---------------------------------------------------------------------------

def test_parse_cdn_url_prefers_signed_over_bare():
    html = (
        '<a href="https://sto.romsfast.com/Wii/Game%20(USA).zip">stale</a>'
        '<a href="https://sto.romsfast.com/Wii/Game%20(USA).zip?token=ABC123%3D%3D">fresh</a>'
    )
    assert _parse_cdn_url(html) == "https://sto.romsfast.com/Wii/Game%20(USA).zip?token=ABC123%3D%3D"


def test_parse_cdn_url_falls_back_to_bare_url():
    html = '<a href="https://cdn.example.com/roms/Game.7z">only</a>'
    assert _parse_cdn_url(html) == "https://cdn.example.com/roms/Game.7z"


def test_parse_cdn_url_none_when_absent():
    assert _parse_cdn_url("<html><body>no download links here</body></html>") is None


# ---------------------------------------------------------------------------
# configure() — FlareSolverr URL wiring
# ---------------------------------------------------------------------------

def test_configure_sets_and_trims_flaresolverr_url():
    s = RomsfunSource()
    s.configure({"flaresolverr_url": "http://192.168.0.81:8191/"})
    assert s._flaresolverr_url == "http://192.168.0.81:8191"


def test_configure_blank_disables_solver():
    s = RomsfunSource()
    s.configure({"flaresolverr_url": ""})
    assert s._flaresolverr_url == ""


def test_default_instance_has_no_solver():
    assert RomsfunSource()._flaresolverr_url == ""
